#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""每日发现 AI + 人文场景的新 GitHub 开源项目，更新 README.md 并推送到 main。

仅使用 Python 标准库，无第三方依赖。

    python scripts/discover.py --dry-run    # 只列候选，不改文件、不提交
    python scripts/discover.py --quick      # 每个场景只跑第一条检索式（快速验证）
    python scripts/discover.py              # 正式运行：改 README + commit + push
    python scripts/discover.py --exclude owner/repo   # 排除试跑时判定不相关的候选，可重复

可选环境变量：
    GITHUB_TOKEN / GH_TOKEN   GitHub Personal Access Token，仅用于提高 Search API 限额

收录口径（比人工清单更保守，避免误判直接公开）：
    - 只收创建于最近 CREATED_WITHIN_DAYS 天内的仓库
    - star >= MIN_STARS、有描述、有明确开源许可、非 fork / archived / disabled
    - 仓库名 + 描述必须命中该场景的 SCENE_KEYWORDS（否则视为泛 AI 项目丢弃）
    - 命中 EXCLUDE_TERMS 的开发工具类项目一律丢弃
    - 排除 awesome-* 资源清单、已知误判黑名单、以及 README 里已收录过的 URL
    - 每个场景单次最多新增 PER_SCENE_LIMIT 条
"""
from __future__ import annotations

import argparse
import datetime as dt
import json
import os
import re
import subprocess
import sys
import time
import urllib.error
import urllib.parse
import urllib.request

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
README = os.path.join(ROOT, "README.md")
API = "https://api.github.com/search/repositories"
TOKEN = os.getenv("GITHUB_TOKEN") or os.getenv("GH_TOKEN") or ""

MIN_STARS = 30
CREATED_WITHIN_DAYS = 365
PER_QUERY = 15
PER_SCENE_LIMIT = 2
REQUEST_INTERVAL = 2.5 if TOKEN else 7.0

SCENE_QUERIES = {
    "Companionship": ["ai companion", "ai virtual pet"],
    "Social Robots": ["companion robot", "social robot"],
    "Elderly Care": ["elderly care ai", "dementia ai"],
    "Parenting": ["ai parenting", "children ai companion"],
    "Social Interaction": ["social simulation llm", "llm agent society"],
    "Emotional Support": ["empathetic dialogue", "mental health llm"],
    "Memory and Narrative": ["digital legacy ai", "life story ai"],
    "Accessibility": ["assistive technology ai", "accessibility llm"],
}

BLACKLIST = {
    "grades2018/awesome-memoir-writing-tools",
    "davidpelayo/awesome-parenting",
    "athen-player1/awesome-parenting",
    "hollyadele/awesome-programming-for-kids",
    "orbitalturtle/awesome-ai-parenting",
    "microsoft/hax-toolkit",
}

SCENE_KEYWORDS = {
    "Companionship": ["companion", "virtual pet", "desktop pet", "vtuber", "roleplay",
                      "character card", "桌宠", "陪伴"],
    "Social Robots": ["companion robot", "social robot", "desktop robot", "humanoid", "机器人"],
    "Elderly Care": ["elderly", "senior care", "dementia", "alzheimer", "aging care",
                     "养老", "认知症", "老年"],
    "Parenting": ["parenting", "children", "child ", "kids", "bedtime story",
                  "育儿", "儿童", "亲子"],
    "Social Interaction": ["social simulation", "agent society", "social agent",
                           "human behavior simulation", "社交模拟", "社会模拟"],
    "Emotional Support": ["empathetic", "emotional support", "mental health", "counsel",
                          "共情", "心理支持", "情感支持"],
    "Memory and Narrative": ["memoir", "life story", "digital legacy", "oral history",
                             "family history", "life log", "回忆录", "口述史"],
    "Accessibility": ["accessibility", "assistive", "screen reader", "disability",
                      "无障碍", "视障", "辅具"],
}

EXCLUDE_TERMS = [
    "claude code", "coding agent", "mcp server", "spec-driven", "cursor", "copilot",
    "construction", "boilerplate", "template repo", "sdk for developers",
]

SECTION_RE = re.compile(r"^## (.+?)\s*$")
URL_RE = re.compile(r"https://github\.com/[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+", re.I)
COUNT_RE = re.compile(r"^\*\*(\d+) projects\*\*")


def existing_urls(text: str) -> set[str]:
    return {u.lower().rstrip("/") for u in URL_RE.findall(text)}


def search(query: str, since: str) -> list[dict]:
    params = {
        "q": f"{query} in:name,description created:>{since} fork:false",
        "sort": "stars",
        "order": "desc",
        "per_page": str(PER_QUERY),
    }
    url = API + "?" + urllib.parse.urlencode(params)
    headers = {
        "Accept": "application/vnd.github+json",
        "X-GitHub-Api-Version": "2022-11-28",
        "User-Agent": "awesome-ai-humanistic-scenes-daily-discover",
    }
    if TOKEN:
        headers["Authorization"] = "Bearer " + TOKEN

    for attempt in range(3):
        req = urllib.request.Request(url, headers=headers)
        try:
            with urllib.request.urlopen(req, timeout=25) as resp:
                return json.load(resp).get("items", [])
        except urllib.error.HTTPError as e:
            body = e.read().decode("utf-8", "replace")[:200]
            if e.code in (403, 429):
                raw = e.headers.get("Retry-After")
                try:
                    wait = int(raw) if raw else 20 * (attempt + 1)
                except ValueError:
                    wait = 20 * (attempt + 1)
                print(f"  ! 限流/拒绝 HTTP {e.code}，等待 {wait}s 重试：{body}")
                time.sleep(wait)
                continue
            print(f"  ! 检索失败 HTTP {e.code}: {body}")
            return []
        except Exception as e:
            print(f"  ! 网络异常：{e}")
            return []
    return []


def eligible(repo: dict, seen: set[str], scene: str) -> dict | None:
    full = repo.get("full_name") or ""
    if "/" not in full:
        return None
    url = f"https://github.com/{full}"
    if url.lower() in seen or full.lower() in BLACKLIST:
        return None

    low = (repo.get("name") or "").lower()
    if low == "awesome" or low.startswith("awesome-"):
        return None
    if repo.get("fork") or repo.get("archived") or repo.get("disabled"):
        return None

    desc = (repo.get("description") or "").strip()
    if len(desc) < 10:
        return None

    haystack = re.sub(r"\s+", " ", low + " " + desc).lower()
    if not any(k in haystack for k in SCENE_KEYWORDS[scene]):
        return None
    if any(t in haystack for t in EXCLUDE_TERMS):
        return None

    if (repo.get("stargazers_count") or 0) < MIN_STARS:
        return None
    lic = ((repo.get("license") or {}).get("spdx_id") or "").upper()
    if lic in ("", "NOASSERTION", "NONE"):
        return None

    pushed = repo.get("pushed_at") or ""
    try:
        if pushed:
            age = dt.datetime.now(dt.timezone.utc) - dt.datetime.fromisoformat(
                pushed.replace("Z", "+00:00")
            )
            if age.days > 365:
                return None
    except ValueError:
        pass

    desc = re.sub(r"\s+", " ", desc).replace("[", "").replace("]", "").strip()
    desc = desc.rstrip(" .;。；") + "."
    if len(desc) > 160:
        desc = desc[:157].rstrip() + "..."
    return {"full": full, "url": url, "desc": desc, "stars": repo.get("stargazers_count", 0)}


def apply_additions(text: str, additions: dict[str, list[dict]]) -> str:
    lines = text.splitlines()
    heads = [(i, m.group(1)) for i, ln in enumerate(lines) if (m := SECTION_RE.match(ln))]
    start_of = {name: i for i, name in heads}

    for scene in sorted(additions, key=lambda s: -start_of[s]):
        start = start_of[scene]
        end = next((i for i, _ in heads if i > start), len(lines))
        sep = next((i for i in range(start + 1, end) if lines[i].strip() == "* * *"), end)
        ins = sep
        while ins - 1 > start and lines[ins - 1].strip() == "":
            ins -= 1
        if scene == "Accessibility":
            for j in range(start + 1, ins):
                if "未检索到符合收录标准" in lines[j]:
                    lines[j] = "面向无障碍与特殊人群的开源辅助工具与适配项目。"
        lines[ins:ins] = [
            f"- [{e['full']}]({e['url']}) - {e['desc']}" for e in additions[scene]
        ]

    total = sum(len(v) for v in additions.values())
    for i, ln in enumerate(lines):
        m = COUNT_RE.match(ln)
        if m:
            lines[i] = ln.replace(
                f"**{m.group(1)} projects**",
                f"**{int(m.group(1)) + total} projects**",
                1,
            )
            break
    return "\n".join(lines).rstrip("\n") + "\n"


def git(*args: str) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["git", "-C", ROOT, *args],
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
    )


def commit_and_push(entries: list[dict]) -> bool:
    if git("diff", "--quiet", "README.md").returncode == 0:
        print("README 无变化，跳过提交。")
        return True
    git("add", "README.md")
    if git("diff", "--cached", "--quiet").returncode == 0:
        print("无待提交变更。")
        return True

    names = ", ".join(e["full"] for e in entries)[:60]
    date = dt.date.today().isoformat()
    r = git("commit", "-m", f"chore: auto-add {names} ({date}) [skip ci]")
    if r.returncode != 0:
        print("提交失败：" + (r.stderr or r.stdout).strip())
        return False

    if not git("remote").stdout.strip():
        print("未配置 git remote：已本地提交，但未推送。请先执行 git remote add origin <url> 并配置凭证。")
        return False
    p = git("push", "origin", "HEAD:main")
    if p.returncode != 0:
        print("推送失败：" + (p.stderr or p.stdout).strip())
        return False
    print("已提交并推送到 main。")
    return True


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry-run", action="store_true", help="只列候选，不写文件、不提交")
    ap.add_argument("--quick", action="store_true", help="每个场景只跑第一条检索式")
    ap.add_argument("--exclude", action="append", default=[], metavar="OWNER/REPO",
                    help="排除指定仓库，可重复传入")
    args = ap.parse_args()

    with open(README, encoding="utf-8") as f:
        text = f.read()
    seen = existing_urls(text)
    for item in args.exclude:
        seen.add(f"https://github.com/{item.strip().strip('/')}".lower())
    since = (dt.date.today() - dt.timedelta(days=CREATED_WITHIN_DAYS)).isoformat()
    print(f"现有项目 URL：{len(seen)}")
    print(f"检索窗口：created:>{since} | star >= {MIN_STARS} | 每场景最多 {PER_SCENE_LIMIT} 条")
    print(f"认证：{'Token' if TOKEN else '匿名（10 次/分）'} | 请求间隔 {REQUEST_INTERVAL}s\n")

    additions: dict[str, list[dict]] = {}
    for scene, queries in SCENE_QUERIES.items():
        found: list[dict] = []
        for q in queries[:1] if args.quick else queries:
            print(f"  [{scene}] {q}")
            for repo in search(q, since):
                info = eligible(repo, seen, scene)
                if info:
                    seen.add(info["url"].lower())
                    found.append(info)
                    if len(found) >= PER_SCENE_LIMIT:
                        break
            time.sleep(REQUEST_INTERVAL)
            if len(found) >= PER_SCENE_LIMIT:
                break
        if found:
            additions[scene] = found

    total = sum(len(v) for v in additions.values())
    if not total:
        print("\n无新增项目，仓库保持不变。")
        return 0

    print("\n将新增：")
    for scene, entries in additions.items():
        for e in entries:
            print(f"  + {scene}: {e['full']} ({e['stars']}★) - {e['desc']}")
    if args.dry_run:
        print("\n[dry-run] 未修改任何文件、未提交。")
        return 0

    with open(README, "w", encoding="utf-8", newline="\n") as f:
        f.write(apply_additions(text, additions))
    print(f"\n已更新 README.md，新增 {total} 条。")
    flat = [e for v in additions.values() for e in v]
    return 0 if commit_and_push(flat) else 1


if __name__ == "__main__":
    sys.exit(main())
