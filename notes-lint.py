#!/usr/bin/env python3
"""notes-lint.py — 查我自己的记忆/笔记里的指针有没有断。

三类检查：
  1) 路径指针：notes/… skills/… tools/… labs/… memory/… cron/… 等，文件不在就 FAIL。
  2) 节号指针：「第 N 节」配同一行/同段里出现的 notes/*.md，核对那份文件真有 `## N.`。
     没有文件名上下文时，去全部 notes/nightly/*.md 里找，找不到才 FAIL，找到多份只 WARN。
  3) 节号自身：同一份笔记里编号重复 → WARN（我经常顺手续写到旧节）。

用法：
  ./notes-lint.py                 # 查 MEMORY/AGENTS/SOUL/USER + notes/**.md
  ./notes-lint.py --scope prompts # 只查提示词四件套
  ./notes-lint.py --scope notes   # 只查笔记
  ./notes-lint.py --json
  ./notes-lint.py --quiet         # 只报 FAIL/WARN 摘要（默认就是这个）
退出码：有 FAIL 则 1。
"""
from __future__ import annotations

import argparse
import glob
import json
import os
import re
import sys

WS = os.path.dirname(os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))
PROMPT_FILES = ["MEMORY.md", "AGENTS.md", "SOUL.md", "USER.md"]
MEM_DIR = os.path.join(WS, "memory")

PATH_RE = re.compile(
    r"(?<![\w/~.-])("
    r"(?:~/\.nanobot/|notes/|skills/|memory/|cron/|triggers/|labs/|tools/)"
    r"[A-Za-z0-9_./%-]*[A-Za-z0-9_%*/])"
)
SECTION_RE = re.compile(r"第\s*(\d+)\s*节")
MD_RE = re.compile(r"(`?)((?:notes|memory)/[A-Za-z0-9_./%-]+\.md)\1")
HEAD_RE = re.compile(r"^#{2,3}\s*(\d+)\.\s", re.M)
HEAD2_RE = re.compile(r"^##\s*(\d+)\.\s", re.M)
DATE_HEAD_RE = re.compile(r"^#{2,3}\s*(\d{4}-\d{2}-\d{2})")
LINE_DATE_RE = re.compile(r"20\d{2}-\d{2}-\d{2}")


def strip_code_span(line: str) -> str:
    return line


SITE_PKG = None
for cand in glob.glob(os.path.expanduser("~/.nanobot/venv/lib/python3*/site-packages/nanobot")):
    SITE_PKG = cand
    break

def build_bases():
    bases = [WS, os.path.join(WS, "notes", "nightly"), os.path.join(WS, "notes")]
    if SITE_PKG:
        bases.append(SITE_PKG)
    return bases


CANDIDATE_BASES = build_bases()


def resolve(p: str):
    """把一个指针展开成多个候选绝对路径（笔记里 labs/ 是相对 notes/nightly/，
    cron/service.py 这类是镜像源码里的包路径）。"""
    if p.startswith("~/"):
        return [os.path.expanduser(p)]
    if p.startswith("/"):
        return [p]
    return [os.path.join(b, p) for b in CANDIDATE_BASES]


def exists_any(pats) -> bool:
    return any(exists(x) for x in pats)


def exists(pat: str) -> bool:
    if any(ch in pat for ch in "*?"):
        return bool(glob.glob(pat))
    return os.path.exists(pat)


def load_texts(scope: str):
    """返回 [(显示名, 绝对路径, 文本)]。"""
    out = []
    if scope in ("prompts", "all"):
        for name in PROMPT_FILES:
            p = os.path.join(WS, name)
            if os.path.exists(p):
                out.append((name, p, open(p, encoding="utf-8", errors="replace").read()))
        # memory/history.jsonl 之类不查，只查 md
    if scope in ("notes", "all"):
        for p in sorted(glob.glob(os.path.join(WS, "notes", "**", "*.md"), recursive=True)):
            rel = os.path.relpath(p, WS)
            out.append((rel, p, open(p, encoding="utf-8", errors="replace").read()))
    return out


def sections_of(path: str):
    try:
        t = open(path, encoding="utf-8", errors="replace").read()
    except OSError:
        return set()
    return {int(m) for m in HEAD_RE.findall(t)}


_section_cache: dict[str, set[int]] = {}


def section_set(path: str) -> set[int]:
    if path not in _section_cache:
        _section_cache[path] = sections_of(path)
    return _section_cache[path]


def all_note_sections():
    idx = {}
    for p in glob.glob(os.path.join(WS, "notes", "**", "*.md"), recursive=True):
        for n in section_set(p):
            idx.setdefault(n, []).append(os.path.relpath(p, WS))
    return idx


DATED_NOTE_RE = re.compile(r"^(\d{4}-\d{2}-\d{2})\.md$")


def date_blocks(text: str):
    """文件里 「## 2026-10-31 20:30 新增」这类日期小标题的位置，用于给无主指针定日期。"""
    out = []
    for i, line in enumerate(text.splitlines(), 1):
        m = DATE_HEAD_RE.match(line)
        if m:
            out.append((i, m.group(1)))
    return out


def block_date(blocks, lineno: int, line: str = "", pos: int | None = None):
    """定日期：优先取条目里「第 N 节」**之前最近**的那个 YYYY-MM-DD
    （THREADS 写成「（2026-10-25 12:10，第 92 节）」「2026-10-26 00:40 后续已了（第 105 节）」），
    行内没日期才回退到所在日期块的小标题。取第一个日期会把事件日期（如「PRL 2026-10-21」）当成笔记日期，踩过。"""
    if pos is None:
        pos = len(line)
    best = None
    for m in LINE_DATE_RE.finditer(line):
        if m.start() < pos:
            best = m.group(0)
    if best:
        return best
    d = None
    for ln, ds in blocks:
        if ln <= lineno:
            d = ds
        else:
            break
    return d


def check(scope: str):
    problems = []
    stats = {"files": 0, "paths": 0, "sections": 0}
    texts = load_texts(scope)
    stats["files"] = len(texts)
    notefiles = [rel for rel, _, _ in texts if rel.startswith("notes/")]
    note_sections = None

    # ---- 1) 路径指针
    for name, path, text in texts:
        for lineno, line in enumerate(text.splitlines(), 1):
            for m in PATH_RE.finditer(line):
                stats["paths"] += 1
                raw = m.group(1).rstrip("，。、；：)】」'\"")
                if "..." in raw or "<" in raw or "YYYY" in raw:
                    continue
                if raw.rstrip("/") in ("notes", "skills", "tools", "notes/nightly", "notes/qq"):
                    continue
                if exists_any(resolve(raw)):
                    continue
                # 散文里截断的片段（cron/trigger、logs/group<群号>）不算断链
                tail = line[m.end():m.end() + 1]
                if tail.startswith("<") or exists_any([x + "*" for x in resolve(raw)]):
                    continue
                # 「cron/trigger 主动轮」这类散文复合词：无扩展名且后面直接跟中文
                last = raw.rsplit("/", 1)[-1]
                rest = line[m.end():].lstrip(" ")
                if "." not in last and rest[:1] and ord(rest[0]) > 0x2E80:
                    continue
                # tools/ 常有 “tools/xxx.py（工具）” 以外写法，允许在 tools 目录里按裸名找
                base = os.path.basename(raw.rstrip("/"))
                if base and os.path.exists(os.path.join(WS, "notes/nightly/tools", base)):
                    continue
                problems.append({
                    "level": "FAIL" if not name.startswith("notes/") else "WARN",
                    "kind": "path", "file": name, "line": lineno,
                    "msg": f"路径不存在：{raw}",
                })

    # ---- 2) 节号指针
    for name, path, text in texts:
        # 非按日归档的文件（THREADS.md、MEMORY.md …）里，日期块给「当天笔记第 N 节」定日期
        self_dated = bool(DATED_NOTE_RE.match(os.path.basename(path)))
        blocks = [] if self_dated else date_blocks(text)
        for lineno, line in enumerate(text.splitlines(), 1):
            secs = SECTION_RE.finditer(line)
            secs = list(secs)
            if not secs:
                continue
            md = [(m.start(), m.group(2)) for m in MD_RE.finditer(line)]
            for sm in secs:
                stats["sections"] += 1
                n = sm.group(1)
                before = [t for pos, t in md if pos < sm.start()]
                mdfiles = [before[-1]] if before else []
                n = int(n)
                if mdfiles:
                    for mf in mdfiles:
                        mp_any = resolve(mf.strip("`"))
                        mp = next((x for x in mp_any if os.path.exists(x)), None)
                        if mp is None:
                            continue  # 路径检查已经报过
                        if n not in section_set(mp):
                            problems.append({
                                "level": "FAIL" if not name.startswith("notes/") else "WARN",
                                "kind": "section", "file": name, "line": lineno,
                                "msg": f"第 {n} 节在 {mf} 里不存在",
                            })
                else:
                    if n in section_set(path):
                        continue  # 指向自己这份笔记的某一节
                    d = block_date(blocks, lineno, line, sm.start()) if blocks else None
                    if d:
                        target = os.path.join(WS, "notes", "nightly", d + ".md")
                        if os.path.exists(target):
                            if n not in section_set(target):
                                problems.append({
                                    "level": "WARN", "kind": "section", "file": name, "line": lineno,
                                    "msg": f"第 {n} 节在 notes/nightly/{d}.md 里不存在（按本文件日期块推断）",
                                })
                            continue
                    if note_sections is None:
                        note_sections = all_note_sections()
                    hits = note_sections.get(n, [])
                    if not hits:
                        problems.append({
                            "level": "FAIL" if not name.startswith("notes/") else "WARN",
                            "kind": "section-orphan", "file": name, "line": lineno,
                            "msg": f"«第 {n} 节» 没有任何笔记文件含该编号",
                        })
                    elif len(hits) > 1:
                        problems.append({
                            "level": "WARN", "kind": "section-ambig", "file": name, "line": lineno,
                            "msg": f"«第 {n} 节» 在 {len(hits)} 份笔记里都有：" + ", ".join(hits[:4]),
                        })

    # ---- 3) 单文件内节号重复
    for name, path, text in texts:
        if not name.startswith("notes/"):
            continue
        seen = {}
        for m in HEAD2_RE.finditer(text):
            n = int(m.group(1))
            seen[n] = seen.get(n, 0) + 1
        for n, c in sorted(seen.items()):
            if c > 1:
                problems.append({
                    "level": "WARN", "kind": "dup-section", "file": name, "line": 0,
                    "msg": f"节号 {n} 出现 {c} 次",
                })
    return problems, stats


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--scope", choices=["all", "prompts", "notes"], default="all")
    ap.add_argument("--json", action="store_true")
    ap.add_argument("--ws", help="覆盖 workspace 根（自测用）")
    ap.add_argument("--verbose", "-v", action="store_true", help="逐个列出，默认只列一例每类")
    args = ap.parse_args()

    global WS, CANDIDATE_BASES
    if args.ws:
        WS = os.path.abspath(args.ws)
        CANDIDATE_BASES = build_bases()

    problems, stats = check(args.scope)
    fails = [p for p in problems if p["level"] == "FAIL"]
    warns = [p for p in problems if p["level"] == "WARN"]

    if args.json:
        print(json.dumps({"fail": len(fails), "warn": len(warns), "stats": stats,
                          "problems": problems},
                         ensure_ascii=False, indent=1))
    else:
        shown = problems if args.verbose else problems[:40]
        for p in shown:
            print(f"{p['level']:4} {p['file']}:{p['line']} [{p['kind']}] {p['msg']}")
        if len(problems) > len(shown):
            print(f"… 另有 {len(problems) - len(shown)} 条（--verbose 看全）")
        print(f"\n{stats['files']} 个文件 / {stats['paths']} 条路径指针 / "
              f"{stats['sections']} 条节号指针 → FAIL {len(fails)} / WARN {len(warns)}")
    return 1 if fails else 0


if __name__ == "__main__":
    sys.exit(main())
