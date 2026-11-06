#!/usr/bin/env python3
"""mem-audit.py — 审计 MEMORY.md 条目的「新鲜度」。

思路（Zep-lite / 衰减淘汰的落地版）：
  把 MEMORY.md 拆成条目 → 抽特征 token（反引号代码、**粗体**、ASCII 词、CJK 4-gram）
  → 在 ~/.nanobot/sessions/*.jsonl 里找命中 → 记录最后命中时间。
  命中口径（2026-10-23 改）：只有 **user 消息**算真信号；我自己回复里提到条目会把
  它重新加热（实测单条 242 次命中全是我在讨论它），assistant 命中单列一栏不参与 age_user。
  区分 dream 会话（条目是 Dream 自己写进去的，命中=自回声）与真实会话
  （telegram / websocket / heartbeat），后者才是「主人实际还在用」的信号。

用法：
  ./mem-audit.py                 # 文本表，最老的排前面
  ./mem-audit.py --json          # 机器可读
  ./mem-audit.py --min-age 7     # 只看 ≥7 天没被真实会话碰过的
  ./mem-audit.py --entry 5       # 展开第 5 条（看它的 token 和命中分布）

退出码：0 正常；1 有 ≥min-age 天的陈旧条目（方便挂 heartbeat）。
"""
from __future__ import annotations

import argparse
import base64
import json
import os
import re
import sys
from datetime import datetime

WS = os.path.expanduser("~/.nanobot/workspace")
MEM = os.path.join(WS, "memory", "MEMORY.md")
SESS = os.path.expanduser("~/.nanobot/sessions")

STOP = set(
    """also been both each from have here into more must only over same some such than that their them then there these they this those very were what when where which while will with your
    true false none null self args kwargs print import return def class""".split()
)


def parse_entries(text: str) -> list[dict]:
    """按 col-0 的 '- ' 切条目；缩进行归属上一条。"""
    lines = text.splitlines()
    entries, cur, sec = [], None, ""
    for ln in lines:
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
            # 空行后紧跟非缩进内容 => 条目结束
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
        title = e["lines"][0][2:].strip()
        title = re.sub(r"\*\*|`", "", title)
        out.append(
            {
                "idx": i,
                "section": e["section"],
                "title": title[:90],
                "body": body,
            }
        )
    return out


CJK = r"\u4e00-\u9fff"


def tokens_of(body: str, limit: int = 30) -> list[str]:
    toks: list[str] = []

    def add(t: str):
        t = t.strip()
        if len(t) < 2:
            return
        if t.lower() in STOP:
            return
        if t not in toks:
            toks.append(t)

    for t in re.findall(r"`([^`\n]{2,60})`", body):
        for part in re.split(r"[\s/]+", t):
            add(part)
    for t in re.findall(r"\*\*([^*\n]{2,40})\*\*", body):
        add(re.sub(r"\s+", "", t))
    # 普通英文单词几乎都启不到作用：cron/心跳提示词里的 active/task/continue
    # 会把一大片条目全点亮。只留“看着像标识符”的：带 _ . / - 数字，或原本就是混写。
    for w in re.findall(r"[A-Za-z][A-Za-z0-9_.\-/]{3,}", body):
        if re.search(r"[_.\-/0-9]", w) or (w.lower() != w and w.upper() != w):
            add(w.lower())
    # CJK：整段 ≤8 字直接用，长的切 4-gram
    for run in re.findall(f"[{CJK}]{{2,}}", body):
        if len(run) <= 8:
            add(run)
        else:
            for i in range(0, len(run) - 3, 3):
                add(run[i : i + 4])
    return toks[:limit]


def load_records(sessions_dir: str, drop_templates: bool = True) -> list[dict]:
    recs = []
    for dirpath, _dirnames, files in os.walk(sessions_dir):
        for fn in files:
            if not fn.endswith(".jsonl"):
                continue
            path = os.path.join(dirpath, fn)
            key = fn[:-6]
            try:
                kind = base64.b64decode(key + "==").decode("utf-8", "replace")
            except Exception:
                kind = key
            if kind.startswith("dream:"):
                family = "dream"
            elif kind.startswith("heartbeat"):
                family = "heartbeat"
            elif kind.startswith("telegram:"):
                family = "telegram"
            elif kind.startswith("websocket:"):
                family = "webui"
            else:
                family = "other"
            with open(path, encoding="utf-8", errors="replace") as fh:
                for line in fh:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        o = json.loads(line)
                    except Exception:
                        continue
                    if o.get("_type") == "metadata" and "content" not in o:
                        continue
                    ts = o.get("timestamp") or o.get("created_at") or ""
                    role = o.get("role") or ""
                    if role not in ("user", "assistant"):
                        # tool 消息里全是命令输出/文件回显（我 cat 一遍 MEMORY.md 就会
                        # 让所有 token 都命中一次），只保留对话文本才有信号。
                        continue
                    content = o.get("content")
                    if isinstance(content, list):
                        content = " ".join(
                            str(c.get("text", "")) if isinstance(c, dict) else str(c)
                            for c in content
                        )
                    if not content:
                        continue
                    recs.append(
                        {
                            "family": family,
                            "role": role,
                            "ts": str(ts),
                            "text": str(content).lower(),
                        }
                    )
    return recs


def template_prefixes(recs: list[dict], min_count: int = 5) -> set[str]:
    """重复≥N 次的 user 文本开头 = 模板（cron 提示词、Dream/心跳提示词），不是真提问。"""
    cnt: dict[str, int] = {}
    for r in recs:
        if r["role"] != "user":
            continue
        cnt[r["text"][:100]] = cnt.get(r["text"][:100], 0) + 1
    return {k for k, v in cnt.items() if v >= min_count}


def audit(mem_path: str, sessions_dir: str, now: datetime | None = None,
          mode: str = "strict", drop_templates: bool = True) -> list[dict]:
    now = now or datetime.now()
    text = open(mem_path, encoding="utf-8").read()
    entries = parse_entries(text)
    recs = load_records(sessions_dir)
    tmpl = template_prefixes(recs) if drop_templates else set()
    n_tmpl = sum(1 for r in recs if r["role"] == "user" and r["text"][:100] in tmpl)

    # token 的跨条目文档频率：出现 <=2 个条目才算「有辨识度」，
    # 否则像 gateway / http / 2026-10 这种词谁都能命中，命中数毫无信息量。
    all_toks = [tokens_of(e["body"]) for e in entries]
    df: dict[str, int] = {}
    for ts in all_toks:
        for t in set(ts):
            df[t] = df.get(t, 0) + 1

    results = []
    for e, toks_raw in zip(entries, all_toks):
        toks = [t for t in toks_raw if df[t] <= 2] if mode == "strict" else toks_raw
        if not toks:  # 全是烂大街的词：退回松散模式，但标记出来
            toks = toks_raw
            mode_note = "loose-fallback"
        else:
            mode_note = "strict"
        last_any = last_real = last_asst = None
        hits = hits_real = hits_asst = 0
        matched: dict[str, int] = {}
        fam_last: dict[str, str] = {}
        for r in recs:
            hit_tokens = [t for t in toks if t in r["text"]]
            if not hit_tokens:
                continue
            hits += 1
            ts = r["ts"]
            if ts and (last_any is None or ts > last_any):
                last_any = ts
            if r["family"] in ("telegram", "webui", "heartbeat"):
                # 命中口径：只有「用户说的话」算真信号。我自己写的汇报/摘要会把条目
                # 重新加热（2026-10-23 实测：单条最高 242 次命中，全是我在讨论它），
                # 所以 assistant 命中另计一栏，不参与 age_real。
                if r["role"] == "user" and r["text"][:100] not in tmpl:
                    hits_real += 1
                    if ts and (last_real is None or ts > last_real):
                        last_real = ts
                    fam_last[r["family"]] = max(fam_last.get(r["family"], ""), ts)
                else:
                    hits_asst += 1
                    if ts and (last_asst is None or ts > last_asst):
                        last_asst = ts
            for t in hit_tokens:
                matched[t] = matched.get(t, 0) + 1

        def age(ts):
            if not ts:
                return None
            try:
                return round((now - datetime.fromisoformat(ts)).total_seconds() / 86400, 2)
            except Exception:
                return None

        results.append(
            {
                **e,
                "tokens": toks,
                "mode": mode_note,
                "n_tokens": len(toks),
                "coverage": round(len(matched) / len(toks), 2) if toks else 0.0,
                "hits_all": hits,
                "hits_real": hits_real,
                "hits_assistant": hits_asst,
                "last_any": last_any,
                "last_real": last_real,
                "last_assistant": last_asst,
                "age_any_days": age(last_any),
                "age_real_days": age(last_real),
                "age_assistant_days": age(last_asst),
                "matched_tokens": dict(
                    sorted(matched.items(), key=lambda kv: -kv[1])[:12]
                ),
                "family_last": fam_last,
            }
        )
    return results


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--memory", default=MEM)
    ap.add_argument("--sessions", default=SESS)
    ap.add_argument("--json", action="store_true")
    ap.add_argument("--min-age", type=float, default=None,
                    help="只输出真实会话里 ≥N 天没被提到的条目")
    ap.add_argument("--entry", type=int, default=None, help="展开某条目细节")
    ap.add_argument("--mode", choices=["strict", "loose"], default="strict",
                    help="strict=只用有辨识度的 token（默认），loose=全部 token")
    ap.add_argument("--keep-templates", action="store_true",
                    help="不排除模板化的 user 消息（cron/Dream/心跳提示词会重沨命中）")
    a = ap.parse_args()

    res = audit(a.memory, a.sessions, mode=a.mode, drop_templates=not a.keep_templates)

    def stale(e):
        v = e["age_real_days"]
        return 1e9 if v is None else v

    res_sorted = sorted(res, key=stale, reverse=True)
    if a.entry:
        e = next((x for x in res if x["idx"] == a.entry), None)
        if not e:
            print(f"no entry #{a.entry}", file=sys.stderr)
            return 2
        print(json.dumps(e, ensure_ascii=False, indent=2))
        return 0

    show = res_sorted
    if a.min_age is not None:
        show = [e for e in show if stale(e) >= a.min_age or e["age_real_days"] is None]

    if a.json:
        print(json.dumps(show, ensure_ascii=False, indent=2))
    else:
        print(f"{'#':>3} {'age_user':>8} {'age_asst':>8} {'user':>4} {'asst':>4} {'all':>4} {'cov':>4} {'tok':>3}  {'sec':<12} title")
        for e in show:
            ar = "never" if e["age_real_days"] is None else f"{e['age_real_days']:.1f}d"
            aa = "never" if e["age_assistant_days"] is None else f"{e['age_assistant_days']:.1f}d"
            print(
                f"{e['idx']:>3} {ar:>8} {aa:>8} {e['hits_real']:>4} {e['hits_assistant']:>4} "
                f"{e['hits_all']:>4} {e['coverage']*100:>3.0f}% {e['n_tokens']:>3}  "
                f"{e['section'][:12]:<12} {e['title'][:52]}"
            )
    n_stale = sum(
        1 for e in res if e["age_real_days"] is None or e["age_real_days"] >= (a.min_age or 3)
    )
    if a.min_age is not None and not a.json:
        print(f"\n≥{a.min_age:g} 天未被真实会话提及：{n_stale}/{len(res)} 条")
    return 1 if (a.min_age is not None and n_stale) else 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except BrokenPipeError:  # 管道给 head 时安静退出
        os._exit(0)
