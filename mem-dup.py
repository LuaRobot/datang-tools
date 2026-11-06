#!/usr/bin/env python3
"""mem-dup.py — 找提示词文件里的冗余：近似重复条目、可外链的长条目。

为什么是「冗余」而不是「新鲜度」：2026-10-23 的 mem-refs.py 证明条目新鲜度
测不出淘汰信号（55/55 有锚点的条目两天内全被碰过）。真正每轮都在付钱的是
重复表述和可以搬进笔记的长段。

扫描对象：AGENTS.md / SOUL.md / USER.md / memory/MEMORY.md
（每轮 system prompt 的 4 个可变部分；工具契约与技能摘要不在文件里）

两组信号：
  1. 近似重复 —— 两条条目字符 3-gram Jaccard ≥ 阈值，跨文件也算（同一条规则
     在 USER.md 和 AGENTS.md 各说一遍是典型）。
  2. 可外链 —— 长度 ≥ --min-len 且正文里没有 notes/ 之类的指针，说明它自己就是
     唯一副本，可以搬进笔记、正文留一行指针。

用法：
  ./mem-dup.py                    # 人读报告
  ./mem-dup.py --json             # 机读
  ./mem-dup.py --threshold 0.4 --min-len 300
  ./mem-dup.py --top 20 --show-text
"""
from __future__ import annotations

import argparse
import json
import os
import re
import sys
from itertools import combinations

WORKSPACE = os.path.dirname(os.path.dirname(os.path.dirname(os.path.dirname(
    os.path.abspath(__file__)))))
DEFAULT_FILES = [
    "AGENTS.md",
    "SOUL.md",
    "USER.md",
    "memory/MEMORY.md",
]

BULLET = re.compile(r"^\s*(?:[-*+]|\d+\.)\s")
HEADING = re.compile(r"^(#{1,6})\s+(.*)")
MD_NOISE = re.compile(r"[`*_>#|\[\]()]")
NON_WORD = re.compile(r"[^0-9a-zA-Z\u4e00-\u9fff]+")
POINTER = re.compile(r"(notes/|tools/|笔记|\.md\b|INDEX\.md|SKILL\.md|https?://)")


def normalize(text: str) -> str:
    text = MD_NOISE.sub(" ", text)
    text = NON_WORD.sub("", text)
    return text.lower()


def shingles(norm: str, k: int = 3) -> set[str]:
    if len(norm) < k:
        return {norm} if norm else set()
    return {norm[i:i + k] for i in range(len(norm) - k + 1)}


def jaccard(a: set[str], b: set[str]) -> float:
    if not a or not b:
        return 0.0
    inter = len(a & b)
    if not inter:
        return 0.0
    return inter / (len(a) + len(b) - inter)


def parse_entries(path: str) -> list[dict]:
    """按「bullet 块 / 标题 / 空行」切条目，记录行号与所属标题。"""
    with open(path, encoding="utf-8") as fh:
        lines = fh.read().splitlines()

    entries: list[dict] = []
    section = ""
    cur: list[str] | None = None
    cur_start = 0

    def flush():
        nonlocal cur
        if cur:
            text = "\n".join(cur).strip()
            if text:
                entries.append({
                    "file": path,
                    "line": cur_start,
                    "section": section,
                    "text": text,
                })
        cur = None

    for idx, line in enumerate(lines, 1):
        if HEADING.match(line):
            flush()
            section = HEADING.match(line).group(2).strip()
            continue
        if not line.strip():
            flush()
            continue
        if BULLET.match(line):
            flush()
            cur = [line]
            cur_start = idx
            continue
        # 普通段落行：续写当前块；没有当前块就自己起一块
        if cur is None:
            cur = [line]
            cur_start = idx
        else:
            cur.append(line)
    flush()
    return entries


def body_len(text: str) -> int:
    return len(text)


def analyse(files: list[str], threshold: float, min_len: int, root: str = None) -> dict:
    root = root or WORKSPACE
    entries: list[dict] = []
    for rel in files:
        full = os.path.join(root, rel)
        if not os.path.exists(full):
            continue
        for e in parse_entries(full):
            e["chars"] = body_len(e["text"])
            e["norm"] = normalize(e["text"])
            e["sh"] = shingles(e["norm"])
            entries.append(e)

    # 1) 近似重复对（跨文件也算；太短的条目噪声大，跳过）
    dupes = []
    cand = [e for e in entries if e["chars"] >= 60]
    for a, b in combinations(cand, 2):
        # 快速排除：长度差太多
        if min(a["chars"], b["chars"]) * 3 < max(a["chars"], b["chars"]):
            continue
        j = jaccard(a["sh"], b["sh"])
        if j >= threshold:
            dupes.append({
                "score": round(j, 3),
                "a": {"file": a["file"], "line": a["line"], "chars": a["chars"],
                      "text": a["text"], "section": a["section"]},
                "b": {"file": b["file"], "line": b["line"], "chars": b["chars"],
                      "text": b["text"], "section": b["section"]},
            })
    dupes.sort(key=lambda d: (-d["score"], -min(d["a"]["chars"], d["b"]["chars"])))

    # 2) 可外链长条目（没有任何指针 = 自己就是唯一副本）
    external = []
    for e in entries:
        if e["chars"] < min_len:
            continue
        if POINTER.search(e["text"]):
            continue
        external.append({
            "file": e["file"], "line": e["line"], "chars": e["chars"],
            "section": e["section"],
            "savings": e["chars"] - 120,  # 留一行指针的预算
            "text": e["text"],
        })
    external.sort(key=lambda x: -x["savings"])

    per_file = {}
    for e in entries:
        d = per_file.setdefault(e["file"], {"entries": 0, "chars": 0})
        d["entries"] += 1
        d["chars"] += e["chars"]

    return {
        "files": per_file,
        "entries": len(entries),
        "dupes": dupes,
        "external": external,
        "params": {"threshold": threshold, "min_len": min_len},
    }


def fmt_entry(e: dict, width: int = 110) -> str:
    head = e["text"].splitlines()[0]
    if len(head) > width:
        head = head[:width] + "…"
    return f"{e['file']}:{e['line']} [{e['chars']}字] {head}"


def main() -> int:
    ap = argparse.ArgumentParser(
        description="找提示词文件（AGENTS/SOUL/USER/MEMORY）里的近似重复条目与可外链长条目",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="近似重复按字符 3-gram Jaccard 算；可外链 = 长度达阈值且正文没有任何 notes/ 指针。")
    ap.add_argument("--root", default=None,
                    help="扫描根目录（默认由脚本位置推 workspace，自测时指向临时目录）")
    ap.add_argument("--file", action="append", default=None,
                    help="只扫指定文件（相对 workspace，可多次给）")
    ap.add_argument("--threshold", type=float, default=0.30,
                    help="Jaccard 阈值，默认 0.30（实测 0.30 及以上全是真重复，"
                         "0.22–0.29 开始混进「共用词但讲不同事」的假阳）")
    ap.add_argument("--min-len", type=int, default=400, help="可外链候选的最小字数，默认 400")
    ap.add_argument("--top", type=int, default=15, help="每类最多列几条，默认 15")
    ap.add_argument("--show-text", action="store_true", help="打印条目正文（截断）")
    ap.add_argument("--json", action="store_true", help="输出 JSON")
    args = ap.parse_args()

    files = args.file or DEFAULT_FILES
    root = args.root or WORKSPACE
    res = analyse(files, args.threshold, args.min_len, root)

    if args.json:
        out = json.loads(json.dumps(res))
        if not args.show_text:
            for d in out["dupes"]:
                d["a"].pop("text", None)
                d["b"].pop("text", None)
            for e in out["external"]:
                e.pop("text", None)
        print(json.dumps(out, ensure_ascii=False, indent=2))
        return 0

    total = sum(v["chars"] for v in res["files"].values())
    print(f"扫描 {len(res['files'])} 个文件 / {res['entries']} 条条目 / {total} 字符")
    for rel, d in res["files"].items():
        print(f"  {rel:<18} {d['entries']:>4} 条  {d['chars']:>7} 字")

    dupe_save = sum(min(d["a"]["chars"], d["b"]["chars"]) for d in res["dupes"])
    print(f"\n== 近似重复（Jaccard ≥ {args.threshold}） {len(res['dupes'])} 对"
          f"，删掉较短那份最多省 {dupe_save} 字符 ≈ {dupe_save // 3} tokens/轮")
    for d in res["dupes"][:args.top]:
        print(f"  {d['score']:.2f}  {fmt_entry(d['a'])}\n        {fmt_entry(d['b'])}")
        if args.show_text:
            print(f"        A: {d['a']['text'][:200]}\n        B: {d['b']['text'][:200]}")
    if len(res["dupes"]) > args.top:
        print(f"  …还有 {len(res['dupes']) - args.top} 对（--top 调大）")

    print(f"\n== 可外链长条目（≥{args.min_len} 字且无 notes/ 指针） {len(res['external'])} 条")
    tot_save = 0
    for e in res["external"]:
        tot_save += e["savings"]
        print(f"  -{e['savings']:>5}  {fmt_entry(e)}")
        if args.show_text:
            print(f"          {e['text'][:200]}")
    if res["external"]:
        print(f"  合计最多省 {tot_save} 字符 ≈ {tot_save // 3} tokens/轮")
    return 0


if __name__ == "__main__":
    sys.exit(main())
