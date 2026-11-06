#!/usr/bin/env python3
"""tui-delta-lab.py —— 量「终端屏幕增量编码」的代价（WorldTUI 那套思路的本机复现）

WorldTUI（360M 模型假装成终端）的输入输出是「屏幕网格 + 增量」而非文本流。
它省 token 的两个关键动作：只重画变化的行、滚动时用 COPY 行操作（3 tok/行，
而不是把整行重写一遍）。

这里真开一个 tmux 会话，逐键/逐行发命令，每次之后 capture-pane 拿真实网格，
按三种编码算 token：

  1. full  —— 每帧整屏重画（最笨的 baseline）
  2. rows  —— 只重画变化的行
  3. delta —— 变化行 + 滚动用 COPY

token 计数按它那套词表近似：一个字符 = 1 个 byte token，行号/列号各 1，
操作名（ROW/COPY/CURSOR）各 1。绝对值跟真模型不完全一致，但同一把尺子比
三种编码的相对开销够用。

用法：
  ./tui-delta-lab.py                       # 120x40
  ./tui-delta-lab.py --cols 96 --rows 32 --json
  ./tui-delta-lab.py --keep                # 留着 tmux 会话自己看
"""
import argparse
import json
import os
import subprocess
import sys
import tempfile
import time

TOK_ROW = TOK_IDX = TOK_COPY = 1


def tmux(*args, session):
    return subprocess.run(
        ["tmux", *[a.format(s=session) for a in args]],
        capture_output=True, text=True, check=False,
    )


def new_session(session, cols, rows):
    conf = tempfile.NamedTemporaryFile("w", suffix=".conf", delete=False)
    conf.write("set -g status off\nset -g remain-on-exit on\n")
    conf.close()
    tmux("-f", conf.name, "new-session", "-d", "-s", "{s}",
         "-x", str(cols), "-y", str(rows), session=session)
    return conf.name


def capture(session, cols, rows):
    out = tmux("capture-pane", "-p", "-t", "{s}", session=session).stdout.split("\n")
    if out and out[-1] == "":
        out = out[:-1]
    return [(out[i] if i < len(out) else "")[:cols].ljust(cols) for i in range(rows)]


def _r(row):
    return row.rstrip()


def encode_full(scr):
    return len(scr) * (TOK_ROW + 2 * TOK_IDX) + sum(len(_r(r)) for r in scr)


def encode_rows(prev, nxt):
    cost, changed = 0, 0
    for i, row in enumerate(nxt):
        if prev is None or _r(prev[i]) != _r(row):
            cost += TOK_ROW + TOK_IDX + len(_r(row))
            changed += 1
    return cost, changed


def find_scroll(prev, nxt):
    """nxt 的前 n-k 行基本等于 prev 的第 k..n-1 行 → 整屏上滚 k 行。

    容忍一处不匹配：capture-pane 里光标那行通常是空的，会在最后一行造成
    差一格（踩过：不容差时真实滚动全部漏识别）。
    """
    n = len(prev)
    if len(nxt) != n:
        return 0
    for k in range(1, n):
        miss = 0
        for i in range(n - k):
            a, b = _r(nxt[i]), _r(prev[i + k])
            if a != b and (a or b):
                miss += 1
                if miss > 1:
                    break
        else:
            if n - k >= 2 and any(_r(r) for r in nxt[: n - k]):
                return k
    return 0


def encode_delta(prev, nxt):
    if prev is None:
        c, ch = encode_rows(None, nxt)
        return c, ch, 0
    plain, _ = encode_rows(prev, nxt)
    if plain == 0:
        return 0, 0, 0
    k = find_scroll(prev, nxt)
    if not k:
        return plain, encode_rows(prev, nxt)[1], 0
    n = len(nxt)
    cost = (n - k) * (TOK_ROW + TOK_IDX + TOK_COPY + TOK_IDX)
    changed = 0
    for i in range(n - k, n):
        row = _r(nxt[i])
        if row:
            cost += TOK_ROW + TOK_IDX + len(row)
            changed += 1
    return cost, changed, k


def scenario():
    """(动作, 参数) 序列。动作：type=逐字符发；line=整行发+回车；key=特殊键；stream=只抓屏。"""
    return [
        ("type", "ls -la"),
        ("key", "Enter"),
        ("line", "cat /etc/hosts"),
        ("type", "seq 1 300"),
        ("key", "Enter"),
        ("stream", 2.0),
        ("line", "for i in $(seq 1 200); do echo line $i; sleep 0.05; done"),
        ("stream", 9.0),
        ("key", "C-l"),
        ("line", "seq 1 999"),
        ("stream", 2.0),
    ]


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--cols", type=int, default=120)
    ap.add_argument("--rows", type=int, default=40)
    ap.add_argument("--json", action="store_true")
    ap.add_argument("--keep", action="store_true")
    args = ap.parse_args()

    session = "tuidelta"
    tmux("kill-session", "-t", "{s}", session=session)
    conf = new_session(session, args.cols, args.rows)
    steps = []
    prev = None
    try:
        subprocess.run(["sleep", "0.6"])
        tmux("send-keys", "-t", "{s}", "-l", "export PS1='$ '", session=session)
        tmux("send-keys", "-t", "{s}", "Enter", session=session)
        subprocess.run(["sleep", "0.4"])
        tmux("send-keys", "-t", "{s}", "C-l", session=session)
        subprocess.run(["sleep", "0.3"])
        prev = capture(session, args.cols, args.rows)

        def snap(label):
            nonlocal prev
            nxt = capture(session, args.cols, args.rows)
            r, rch = encode_rows(prev, nxt)
            d, dch, k = encode_delta(prev, nxt)
            steps.append({"key": label, "full": encode_full(nxt), "rows": r,
                          "delta": d, "rows_changed": rch, "delta_changed": dch,
                          "scroll": k})
            prev = nxt

        for act, val in scenario():
            if act == "type":
                for ch in val:
                    tmux("send-keys", "-t", "{s}", "-l", ch, session=session)
                    subprocess.run(["sleep", "0.03"])
                    snap(ch)
            elif act == "line":
                tmux("send-keys", "-t", "{s}", "-l", val, session=session)
                subprocess.run(["sleep", "0.05"])
                snap("<typed>")
                tmux("send-keys", "-t", "{s}", "Enter", session=session)
                subprocess.run(["sleep", "0.18"])
                snap("Enter")
            elif act == "key":
                tmux("send-keys", "-t", "{s}", val, session=session)
                subprocess.run(["sleep", "0.18"])
                snap(val)
            else:  # stream
                t_end = time.time() + float(val)
                while time.time() < t_end:
                    subprocess.run(["sleep", "0.12"])
                    snap("<output>")

        total = {kk: sum(s[kk] for s in steps) for kk in ("full", "rows", "delta")}
        total["steps"] = len(steps)
        total["scrolls"] = sum(1 for s in steps if s["scroll"])
        total["full_over_delta"] = round(total["full"] / max(total["delta"], 1), 2)
        total["full_over_rows"] = round(total["full"] / max(total["rows"], 1), 2)
        total["rows_over_delta"] = round(total["rows"] / max(total["delta"], 1), 2)

        if args.json:
            print(json.dumps({"size": [args.cols, args.rows], "total": total,
                              "steps": steps}, ensure_ascii=False, indent=1))
        else:
            print(f"tmux {args.cols}x{args.rows}，{total['steps']} 次采样/keypress")
            print(f"  整屏重画 full   : {total['full']:>8} tok")
            print(f"  只画变化行 rows : {total['rows']:>8} tok  ({total['full_over_rows']}× 便宜)")
            print(f"  变化行 + COPY   : {total['delta']:>8} tok  ({total['full_over_delta']}× 便宜，比 rows 再省 {total['rows_over_delta']}×)")
            print(f"  识别到滚动 {total['scrolls']} 次")
            outs = [s for s in steps if s["key"] == "<output>"]
            if outs:
                print(f"  输出阶段：{len(outs)} 帧，rows={sum(s['rows'] for s in outs)} delta={sum(s['delta'] for s in outs)} tok")
            print("\n最贵的 5 帧：")
            for s in sorted(steps, key=lambda x: -x["full"])[:5]:
                print(f"  {s['key']!r:>10} full={s['full']:>6} rows={s['rows']:>5} "
                      f"delta={s['delta']:>5} scroll={s['scroll']} changed={s['rows_changed']}")
    finally:
        if not args.keep:
            tmux("kill-session", "-t", "{s}", session=session)
            os.unlink(conf)
    return 0


if __name__ == "__main__":
    sys.exit(main())
