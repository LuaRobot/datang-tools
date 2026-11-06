#!/usr/bin/env python3
"""mem-refs.py — MEMORY 条目的「被真读后感」信号。

起因：mem-audit.py 只能看条目文字有没有出现在**对话文本**里，等于测「我在不在聊这件事」，
测不到「这条记的路径/命令我到底有没有真去读过、跑过」。于是补这条：
  1. 从每条 MEMORY 条目里抽「锚点」——反引号里像路径/文件/命令的字符串
     （含 `/`，或带 .md/.py/.sh/.json/.yaml/.conf/.service 后缀，或带 - 的可执行名）；
  2. 扫所有会话 jsonl 的 tool_calls 参数（read_file / grep / exec / exec_session /
     write_file / apply_patch …），看参数里有没有这个锚点；
  3. 命中即一次「真引用」，记最后时间。读 memory/MEMORY.md 本身不算引用任何条目。

用法：
  ./mem-refs.py                  # 表：锚点数 / 命中次数 / 最后引用 / 标题
  ./mem-refs.py --json
  ./mem-refs.py --unreferenced   # 只列有锚点但一次没被引用过的（淘汰候选）
  ./mem-refs.py --entry 12       # 展开某条的锚点与逐条命中

口径说明：exec 参数里出现路径 ≠ 一定读过它（可能是 rm/cp/ls 的顺带提及），
所以另给 --strict-exec，只认 read_file/grep/exec 里出现 cat|head|tail|grep|rg|sed|less 的
命令中出现锚点的情况。默认宽松（任何工具参数出现即算）。
"""
from __future__ import annotations

import argparse
import glob
import json
import os
import re
import sys
from datetime import datetime

WS = os.path.expanduser("~/.nanobot/workspace")
MEM = os.path.join(WS, "memory", "MEMORY.md")
SESS = os.path.expanduser("~/.nanobot/sessions")

READ_CMDS = re.compile(r"\b(cat|head|tail|grep|rg|sed|less|wc|stat|bat|batcat|sqlite3|jq|find|du|diff|ffprobe|python3?)\b")
PATH_RE = re.compile(r"^[~/][^\s`'\"]*$")
EXT_RE = re.compile(r"\.(md|py|sh|json|jsonl|yaml|yml|conf|service|sqlite3|log|c|h|js|ts)$")
BIN_RE = re.compile(r"^[a-z0-9][a-z0-9_.\-]*\.(sh|py)$")


def parse_entries(text: str) -> list[dict]:
    """与 mem-audit.py 同一套切分：col-0 的 '- ' 起条，缩进行归属上一条。"""
    entries, cur, sec = [], None, ""
    for ln in text.splitlines():
        if ln.startswith("#"):
            sec = ln.lstrip("# ").strip()
            if cur:
                entries.append(cur)
                cur = None
            continue
        if ln.startswith("- "):
            if cur:
                entries.append(cur)
            cur = {"section": sec, "lines": [ln]}
            continue
        if cur is not None:
            if ln.strip() == "":
                cur["lines"].append("")
            elif ln[:1] in (" ", "\t"):
                cur["lines"].append(ln.rstrip())
            else:
                entries.append(cur)
                cur = None
    if cur:
        entries.append(cur)
    out = []
    for i, e in enumerate(entries, 1):
        body = "\n".join(e["lines"]).strip()
        title = re.sub(r"\*\*|`", "", e["lines"][0][2:].strip())
        out.append({"idx": i, "section": e["section"], "title": title[:90], "body": body})
    return out


def anchors_of(body: str) -> list[str]:
    """抽锚点：反引号里的路径/命令优先，退而求其次抓正文里的绝对路径。"""
    cands: list[str] = []
    for t in re.findall(r"`([^`\n]{2,90})`", body):
        t = t.strip()
        for part in re.split(r"[\s;,()]+", t):
            part = part.strip("'\"")
            if not part or len(part) < 4:
                continue
            if PATH_RE.match(part) or "/" in part or EXT_RE.search(part) or BIN_RE.match(part):
                cands.append(part)
    # 正文里裸写的绝对路径（不带反引号的）
    for t in re.findall(r"[~/][\w@./\-]{6,}", body):
        cands.append(t.strip(".,;:)"))
    seen, out = set(), []
    for c in cands:
        c = os.path.normpath(c) if c.startswith(("~", "/")) else c
        if c in seen or c in ("~", "/", "~/.", "~/."):
            continue
        # 太短的公共路径没信号（/tmp、/proc 之类）
        if len(c) < 6 and c.startswith(("/tmp", "/proc", "/etc", "/sys")):
            continue
        # 含中文的「/xxx/中文」多是正文里的并列词被正则吞掉，不是真路径
        if re.search(r"[\u4e00-\u9fff]", c):
            continue
        # glob 占位符没法字面匹配，直接不要
        if "<" in c or "*" in c:
            continue
        # 没有后缀、本机也不存在的「/a/b」几乎都是词与词被斜杠连在一起（如 mention/name/master）
        if not EXT_RE.search(c) and not os.path.exists(os.path.expanduser(c)):
            continue
        seen.add(c)
        out.append(c)
    return out


def iter_tool_calls(sessions_dir: str):
    for path in sorted(glob.glob(os.path.join(sessions_dir, "**", "*.jsonl"), recursive=True)):
        key = os.path.basename(path)[:-6]
        try:
            import base64

            kind = base64.b64decode(key + "==").decode("utf-8", "replace")
        except Exception:
            kind = key
        with open(path, encoding="utf-8", errors="replace") as fh:
            for line in fh:
                line = line.strip()
                if not line:
                    continue
                try:
                    o = json.loads(line)
                except Exception:
                    continue
                ts = o.get("timestamp") or ""
                for tc in o.get("tool_calls") or []:
                    fn = tc.get("function") or {}
                    yield {
                        "session": kind,
                        "ts": str(ts),
                        "name": fn.get("name") or "",
                        "args": str(fn.get("arguments") or ""),
                    }


def variants_of(anchor: str) -> list[str]:
    """同一路径在命令里的不同写法：原样 / 展开 ~ / 只有 basename。

    实测漏报原因：`cd ~/.nanobot; sqlite3 llm_usage.sqlite3` 里根本不会出现
    `~/.nanobot/llm_usage.sqlite3` 这个全路径。basename 只在够长且带后缀时才加，
    否则 `config.json` 这类会把所有条目都点亮。
    """
    out = [anchor]
    if anchor.startswith("~"):
        out.append(os.path.expanduser(anchor))
    base = os.path.basename(anchor)
    if len(base) >= 8 and "." in base and base not in ("MEMORY.md", "state.json"):
        out.append(base)
    return list(dict.fromkeys(out))


def audit(mem_path: str, sessions_dir: str, strict_exec: bool = False,
          now: datetime | None = None) -> list[dict]:
    now = now or datetime.now()
    entries = parse_entries(open(mem_path, encoding="utf-8").read())
    calls = list(iter_tool_calls(sessions_dir))
    res = []
    for e in entries:
        anc = anchors_of(e["body"])
        hit: dict[str, dict] = {}
        for c in calls:
            args = c["args"]
            if "MEMORY.md" in args and not any("MEMORY.md" in a for a in anc):
                # 整篇读完不等于引用了某条
                args_wo = args.replace("MEMORY.md", "")
            else:
                args_wo = args
            if strict_exec:
                if c["name"] in ("read_file", "grep"):
                    pass
                elif c["name"].startswith("exec") and READ_CMDS.search(args_wo):
                    pass
                else:
                    continue
            for a in anc:
                if a and any(v in args_wo for v in variants_of(a)):
                    d = hit.setdefault(a, {"n": 0, "last": "", "tools": {}, "days": set()})
                    d["n"] += 1
                    d["days"].add(c["ts"][:10])
                    d["tools"][c["name"]] = d["tools"].get(c["name"], 0) + 1
                    if c["ts"] > d["last"]:
                        d["last"] = c["ts"]
        last = max((v["last"] for v in hit.values()), default="")
        age = None
        if last:
            try:
                age = round((now - datetime.fromisoformat(last)).total_seconds() / 86400, 2)
            except Exception:
                age = None
        res.append(
            {
                **e,
                "anchors": anc,
                "n_anchors": len(anc),
                "n_refs": sum(v["n"] for v in hit.values()),
                "n_refs_uniq": sum(len(v["days"]) for v in hit.values()),
                "anchors_hit": {k: {"n": v["n"], "days": sorted(v["days"]), "last": v["last"],
                                   "tools": v["tools"]}
                                for k, v in sorted(hit.items(), key=lambda kv: -kv[1]["n"])},
                "anchors_dead": [a for a in anc if a not in hit],
                "last_ref": last,
                "age_ref_days": age,
            }
        )
    return res


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--memory", default=MEM)
    ap.add_argument("--sessions", default=SESS)
    ap.add_argument("--json", action="store_true")
    ap.add_argument("--unreferenced", action="store_true",
                    help="只列有锚点但从未被工具参数碰过的条目")
    ap.add_argument("--strict-exec", action="store_true",
                    help="exec 只在命令里出现读文件类工具时才计入")
    ap.add_argument("--entry", type=int, default=None)
    a = ap.parse_args()

    res = audit(a.memory, a.sessions, strict_exec=a.strict_exec)
    if a.entry:
        e = next((x for x in res if x["idx"] == a.entry), None)
        if not e:
            print(f"no entry #{a.entry}", file=sys.stderr)
            return 2
        print(json.dumps(e, ensure_ascii=False, indent=2))
        return 0

    show = res
    if a.unreferenced:
        show = [e for e in res if e["n_anchors"] and e["n_refs"] == 0]
    show = sorted(show, key=lambda e: (e["n_refs_uniq"], -e["n_anchors"]))

    if a.json:
        print(json.dumps(show, ensure_ascii=False, indent=2))
    else:
        print(f"{'#':>3} {'anc':>3} {'refs':>5} {'days':>5} {'age':>7}  {'sec':<14} title")
        for e in show:
            ag = "never" if e["age_ref_days"] is None else f"{e['age_ref_days']:.1f}d"
            print(f"{e['idx']:>3} {e['n_anchors']:>3} {e['n_refs']:>5} {e['n_refs_uniq']:>5} "
                  f"{ag:>7}  {e['section'][:14]:<14} {e['title'][:50]}")
        no_anchor = sum(1 for e in res if not e["n_anchors"])
        dead = sum(1 for e in res if e["n_anchors"] and e["n_refs"] == 0)
        live = sum(1 for e in res if e["n_refs"])
        print(f"\n{len(res)} 条：有锚点 {len(res)-no_anchor}、被引用过 {live}、"
              f"有锚点但零引用 {dead}、无锚点(纯描述) {no_anchor}")
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except BrokenPipeError:
        os._exit(0)
