#!/usr/bin/env python3
"""cron 巡检：把 cron/jobs.json（意图）和 cron/runs/*.json（事实）对账。

为什么需要它：自排 job 全绑在 websocket:datang-self，跑挂了只在那条会话里留一句话，
没人主动看。这个脚本回答三个问题：
  1. 该跑的 job 真的跑了吗（按 cron 表达式算 24h 内的槽位，跟 run 记录比）？
  2. 跑的结果好吗（status != ok、卡在 queued 的僵尸记录）？
  3. 有没有"影子"状态（job 删了但记录还在、job 在册但从未跑过）？

注意（源码事实，2026-10-22 核对）：
  - jobs.json 的 state.lastRunAtMs / lastStatus / runHistory **只在本 job id 下累积，而且要跑完才落盘**：
    正在跑的那一轮在盘上仍是 null；迁移换 id 后新 job 的 state 从零开始（旧 id 的历史只在 runs/ 里）。
    runHistory 上限 20 条（cron/service.py 的 `_MAX_RUN_HISTORY`）。
  - cron/runs/ 每个 run 一个 json，写于开始（queued）、收尾时覆写；没有任何清理/轮转逻辑
    （utils/run_records.py 只写不删），会一直长。跑到一半被扛只会留下永远 queued 的僵尸记录
    —— 这是本脚本主要要抓的。
  - dream / heartbeat 是系统托管的每 N 小时 job，不写 run 记录；dream 只能靠
    llm_usage.sqlite3 的 source='dream' 反证，heartbeat 无持久痕迹（没任务时直接 return）。

用法：
  ~/.nanobot/venv/bin/python cron-health.py            # 人读
  ~/.nanobot/venv/bin/python cron-health.py --json     # 单行 JSON
  cron-health.py --hours 48                            # 槽位窗口（默认 24）
  cron-health.py --downtime none                       # 关掉停机豁免（自测用）
  cron-health.py --store PATH --runs PATH --usage PATH
退出码：0 全绿；1 有 FAIL。
"""

from __future__ import annotations

import argparse
import json
import os
import sqlite3
import subprocess
import sys
import time
from datetime import datetime, timedelta
from pathlib import Path

HOME = Path.home()
DEFAULT_STORE = HOME / ".nanobot/workspace/cron/jobs.json"
DEFAULT_RUNS = HOME / ".nanobot/workspace/cron/runs"
DEFAULT_USAGE = HOME / ".nanobot/llm_usage.sqlite3"

SLOT_TOL_BEFORE_S = 120      # 槽位前 2 分钟内的完成也算命中
SLOT_TOL_AFTER_S = 20 * 60   # 槽位后 20 分钟内算命中
STUCK_QUEUED_MIN = 20        # queued 超过这么久视为僵尸
DREAM_MAX_AGE_MIN = 3 * 60   # dream 每 2h，超过 3h 没动静算 FAIL
SYSTEM_JOBS = {"dream", "heartbeat"}

checks: list[dict] = []


def add(level: str, name: str, detail: str) -> None:
    checks.append({"level": level, "name": name, "detail": detail})


def fmt_ts(ms: int | None) -> str:
    if not ms:
        return "-"
    return datetime.fromtimestamp(ms / 1000).strftime("%m-%d %H:%M:%S")


def humanize_schedule(sch: dict) -> str:
    kind = sch.get("kind")
    if kind == "every":
        ms = sch.get("everyMs") or 0
        sec = ms / 1000
        if sec % 3600 == 0:
            return f"每 {int(sec // 3600)}h"
        if sec % 60 == 0:
            return f"每 {int(sec // 60)}m"
        return f"每 {int(sec)}s"
    if kind == "cron":
        return f"{sch.get('expr')} ({sch.get('tz') or '本地'})"
    if kind == "at":
        return f"一次性 @ {fmt_ts(sch.get('atMs'))}"
    return str(kind)


def parse_field(expr: str, lo: int, hi: int) -> set[int]:
    """最小 cron 字段解析：*、*/n、a、a-b、逗号列表。够覆盖现有 job。"""
    out: set[int] = set()
    for part in expr.split(","):
        part = part.strip()
        if not part:
            continue
        step = 1
        if "/" in part:
            part, s = part.split("/", 1)
            step = int(s)
        if part in ("*", "?"):
            start, end = lo, hi
        elif "-" in part:
            a, b = part.split("-", 1)
            start, end = int(a), int(b)
        else:
            start = end = int(part)
            if step > 1:
                end = hi
        for v in range(start, end + 1, step):
            if lo <= v <= hi:
                out.add(v)
    return out


def expected_slots(sch: dict, since: datetime, until: datetime) -> list[datetime] | None:
    """返回窗口内应跑的槽位；不支持的表达式返回 None。"""
    kind = sch.get("kind")
    if kind == "every":
        ms = sch.get("everyMs") or 0
        if not ms:
            return None
        out, t = [], since.timestamp()
        end = until.timestamp()
        # 从 since 起按周期推进（与真正实现一样按 next_run 累加，不做相位对齐）
        while t <= end:
            out.append(datetime.fromtimestamp(t))
            t += ms / 1000
        return out
    if kind != "cron":
        return None
    fields = (sch.get("expr") or "").split()
    if len(fields) != 5:
        return None
    minutes = parse_field(fields[0], 0, 59)
    hours = parse_field(fields[1], 0, 23)
    doms = parse_field(fields[2], 1, 31)
    months = parse_field(fields[3], 1, 12)
    dows = parse_field(fields[4], 0, 6) if fields[4] != "*" else set(range(7))
    out = []
    t = since.replace(second=0, microsecond=0)
    while t <= until:
        # cron dow: 0/7=周日；Python weekday(): 周一=0
        cron_dow = (t.weekday() + 1) % 7
        if t.minute in minutes and t.hour in hours and t.day in doms and t.month in months:
            if fields[4] == "*" or cron_dow in dows or (cron_dow == 0 and 7 in dows):
                out.append(t)
        t += timedelta(minutes=1)
    return out


def load_store(path: Path) -> list[dict]:
    raw = json.loads(path.read_text(encoding="utf-8"))
    return raw.get("jobs", []) if isinstance(raw, dict) else list(raw)


def load_runs(runs_dir: Path) -> list[dict]:
    rows = []
    for f in runs_dir.glob("*.json"):
        try:
            d = json.loads(f.read_text(encoding="utf-8"))
        except Exception:
            add("WARN", "runs:bad-json", f"读不了 {f.name}")
            continue
        d["_path"] = f.name
        # 文件名 <job_id>_<epoch_ms>_<hash>.json 里的 ms 是**起跑时刻**，
        # 而 updated_at_ms 是**结束时刻**（自选轮动辄跑 20–50 分钟）。
        # 槽位核对必须用起跑时刻，否则长跑会整片被判「漏跑」。
        d["_start_ms"] = 0
        try:
            d["_start_ms"] = int(f.stem.split("_")[1])
        except (IndexError, ValueError):
            d["_start_ms"] = d.get("updated_at_ms") or 0
        rows.append(d)
    rows.sort(key=lambda r: r.get("updated_at_ms") or 0)
    return rows


def journal_cron_edits(hours: float) -> dict:
    """从 journal 抓每个 job 最近一次 `Cron: updated job '<name>'` 的时间（epoch s）。

    为什么需要：槽位核对按当前表达式回算，但 job 的表达式是会被改的
    （例：self-directed-2h `25 */2 * * *` → self-directed-hourly `25 * * * *`），
    改动之前的「缺槽位」全是假阳性。jobs.json 里的 updatedAtMs 一写状态就变，
    不能当结构改动时间，所以去 journal 找真实改动时刻。
    """
    env = dict(os.environ)
    env.setdefault("XDG_RUNTIME_DIR", f"/run/user/{os.getuid()}")
    cmd = ["journalctl", "--user", "-u", "nanobot-gateway",
           "--since", f"{max(hours, 1) + 1:g} hours ago",
           "--no-pager", "--output=short-iso"]
    try:
        out = subprocess.run(cmd, capture_output=True, text=True, timeout=45, env=env).stdout
    except Exception:
        return {}
    edits = {}
    for line in out.splitlines():
        if "Cron: updated job" not in line or "Tool call:" in line:
            continue
        try:
            ts = datetime.fromisoformat(line.split()[0]).timestamp()
        except Exception:
            continue
        rest = line[line.find("Cron: updated job"):]
        q1 = rest.find("'")
        q2 = rest.find("'", q1 + 1)
        if q1 < 0 or q2 < 0:
            continue
        nm = rest[q1 + 1:q2]
        edits[nm] = max(edits.get(nm, 0.0), ts)
    return edits


def dream_age_min(usage: Path) -> int | None:
    if not usage.exists():
        return None
    try:
        con = sqlite3.connect(f"file:{usage}?mode=ro", uri=True)
        row = con.execute(
            "select max(started_at_ms) from llm_calls where source='dream'"
        ).fetchone()
        con.close()
    except Exception:
        return None
    if not row or not row[0]:
        return None
    return int((time.time() * 1000 - row[0]) / 60000)


def journal_downtime(since_ts: float) -> list[tuple[float, float]]:
    """机器不在线的区间（unix 秒）：上一轮 boot 的最后一条 → 本轮 boot 的第一条。

    用途：整机停电/死机/硬重启期间没跑不是 job 的错，按槽位会连红十几个小时
    （2026-10-27 23:32 卡死 → 次日 12:34 硬重启，self-directed-hourly 一天缺 13 次）。
    读不到 journal 就返回空表，退回老行为。
    """
    try:
        out = subprocess.run(
            ["journalctl", "--list-boots", "--output=json", "--no-pager"],
            capture_output=True, text=True, timeout=15,
        ).stdout
    except Exception:
        return []
    boots: list[tuple[float, float]] = []
    entries: list = []
    try:
        entries = json.loads(out)  # --output=json 是一个 JSON 数组，不是逐行对象
        if not isinstance(entries, list):
            entries = []
    except Exception:
        for line in out.splitlines():
            line = line.strip()
            if line.startswith("{"):
                try:
                    entries.append(json.loads(line))
                except Exception:
                    pass
    for b in entries:
        if not isinstance(b, dict):
            continue
        try:
            first = (b.get("first_entry") or 0) / 1e6
            last = (b.get("last_entry") or 0) / 1e6
        except Exception:
            continue
        if first and last:
            boots.append((first, last))
    boots.sort()
    gaps = []
    for (_, prev_end), (next_start, _) in zip(boots, boots[1:]):
        if next_start - prev_end > 60 and next_start >= since_ts:
            gaps.append((prev_end, next_start))
    return gaps


def gateway_start_ms() -> int:
    """gateway 进程启动时刻（unix ms）；拿不到返回 0。

    用途：区分「真僵尸」和「被 gateway 重启掐断的一轮」。后者不是故障，
    重启后 cron 会照槽位继续跑，把记录当 FAIL 会让巡检每次重启后连红好几天。
    """
    try:
        env = dict(os.environ)
        env.setdefault("XDG_RUNTIME_DIR", f"/run/user/{os.getuid()}")
        out = subprocess.run(
            ["systemctl", "--user", "show", "nanobot-gateway",
             "-p", "ExecMainStartTimestampMonotonic"],
            capture_output=True, text=True, timeout=10, env=env,
        ).stdout
        mono_us = int(out.strip().split("=", 1)[1] or 0)
        if not mono_us:
            return 0
        with open("/proc/uptime") as fh:
            up_s = float(fh.read().split()[0])
        return int((time.time() - (up_s - mono_us / 1e6)) * 1000)
    except Exception:
        return 0


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--store", default=str(DEFAULT_STORE))
    ap.add_argument("--runs", default=str(DEFAULT_RUNS))
    ap.add_argument("--usage", default=str(DEFAULT_USAGE))
    ap.add_argument("--hours", type=float, default=24.0)
    ap.add_argument("--gateway-start-ms", type=int, default=None,
                    help="gateway 启动时刻（unix ms）；0=不判断重启掐断，默认自动探测")
    ap.add_argument("--downtime", choices=("journal", "none"), default="journal",
                    help="停机窗口来源：journal=按 boot 记录豁免停机期漏跑（默认），none=不豁免")
    ap.add_argument("--json", action="store_true")
    args = ap.parse_args()
    if args.gateway_start_ms is None:
        args.gateway_start_ms = gateway_start_ms()

    store_path, runs_dir, usage = Path(args.store), Path(args.runs), Path(args.usage)

    if not store_path.exists():
        add("FAIL", "store", f"找不到 {store_path}")
        return emit(args)
    try:
        jobs = load_store(store_path)
    except Exception as exc:
        add("FAIL", "store", f"解析失败：{exc}")
        return emit(args)
    add("OK", "store", f"{len(jobs)} 个 job，解析正常")

    runs = load_runs(runs_dir)
    if not runs:
        add("WARN", "runs", "没有任何 run 记录")
    add("OK", "runs", f"{len(runs)} 条记录，最新 {fmt_ts(runs[-1].get('updated_at_ms')) if runs else '-'}")

    now = datetime.now()
    since = now - timedelta(hours=args.hours)
    store_ids = {j.get("id") for j in jobs}
    store_names = {j.get("name") for j in jobs}
    edits = journal_cron_edits(args.hours)

    # 1) 僵尸 queued（跑到一半被掐：gateway 重启 / 进程被杀；记录永远不会收尾）
    for r in runs:
        if r.get("status") == "queued":
            age = (time.time() * 1000 - (r.get("updated_at_ms") or 0)) / 60000
            if age > STUCK_QUEUED_MIN:
                started = r.get("updated_at_ms") or 0
                if args.gateway_start_ms and started and args.gateway_start_ms > started:
                    add("WARN", "runs:interrupted",
                        f"{r.get('job_name')} 那一轮被 gateway 重启掐断（{fmt_ts(started)} 起跑，"
                        f"gateway {fmt_ts(args.gateway_start_ms)} 重启），按槽位会重跑")
                else:
                    add("FAIL", "runs:stuck", f"{r.get('job_name')} 卡在 queued {age:.0f} 分钟（{r.get('run_id')}）")
            else:
                add("OK", "runs:running", f"{r.get('job_name')} 正在跑（{age:.0f} 分钟前开始）")

    # 2) 孤儿记录：job 已删，但 run 记录留着（迁移后旧 id 会落这里，属正常，只报 WARN）
    orphan = {r.get("job_id") for r in runs
              if r.get("job_id") not in store_ids and r.get("job_name") not in store_names}
    if orphan:
        add("WARN", "runs:orphan", f"{len(orphan)} 个已删 job 的残留记录：{', '.join(sorted(orphan))}")

    # 3) 逐 job 对账
    for j in jobs:
        name = j.get("name") or j.get("id")
        sch = j.get("schedule") or {}
        sched_txt = humanize_schedule(sch)

        if not j.get("enabled", True):
            add("WARN", "job:disabled", f"{name} 是禁用状态（{sched_txt}），不核对槽位")
            continue

        # 绑定完整性（is_bound_cron_job 的等价判断）
        p = j.get("payload") or {}
        if p.get("kind") == "agent_turn":
            bound = all([p.get("sessionKey"), p.get("originChannel"), p.get("originChatId")]) and not any(
                [p.get("deliver"), p.get("channel"), p.get("to"), p.get("channelMeta")]
            )
            if not bound:
                add("FAIL", "job:binding", f"{name} 不满足会话绑定条件，运行会被 skip 并 disable")

        if name in SYSTEM_JOBS:
            if name == "dream":
                age = dream_age_min(usage)
                if age is None:
                    add("WARN", "job:dream", "读不到 llm_usage，无法确认 dream 是否在跑")
                elif age > DREAM_MAX_AGE_MIN:
                    add("FAIL", "job:dream", f"dream 已 {age/60:.1f} 小时没调用模型（每 2h 应有一次）")
                else:
                    add("OK", "job:dream", f"{sched_txt}，最近一次模型调用 {age:.0f} 分钟前")
            else:
                add("OK", "job:heartbeat", f"{sched_txt}，系统托管无 run 记录（无任务时直接 return，不留痕）")
            continue

        mine = [r for r in runs if r.get("job_id") == j.get("id") or r.get("job_name") == name]
        # job 创建（或旧 id 最后一次运行）之前不算漏跑：迁移换 id 时 createdAt 很新，
        # 但同名旧 job 的 run 记录仍在，取两者的早者当有效起点。
        lower = (j.get("createdAtMs") or 0) / 1000
        mine_ts = [(r.get("updated_at_ms") or 0) / 1000 for r in mine]
        if mine_ts:
            lower = min(lower, min(mine_ts)) if lower else min(mine_ts)
        start_ts = max(since.timestamp(), lower)
        # 任务在最后一次 run 之后被改过（换了表达式/消息）时，改动之前的槽位
        # 不该按新表达式倒推，从改动时刻起算；改动之后的漏跑照旧能抓到。
        updated_ts = (j.get("updatedAtMs") or 0) / 1000
        if updated_ts and mine_ts and updated_ts > max(mine_ts):
            start_ts = max(start_ts, updated_ts)
        # journal 里真实的结构改动时刻优先（表达式改过之后才按新表达式算槽位）
        edit_ts = edits.get(name) or 0.0
        edit_note = ""
        if edit_ts and edit_ts > start_ts:
            start_ts = edit_ts
            edit_note = f"（{datetime.fromtimestamp(edit_ts):%m-%d %H:%M} 改过任务，此前不核槽位）"
        start = datetime.fromtimestamp(start_ts)

        recent = [r for r in mine if (r.get("updated_at_ms") or 0) / 1000 >= start_ts]
        # queued 由上面僵尸检查单独判，这里不重复算失败
        bad = [r for r in recent if r.get("status") not in ("ok", "queued", None)]
        if bad:
            add("FAIL", "job:status", f"{name} 近 {args.hours:g}h 有 {len(bad)} 次非 ok：{bad[-1].get('status')} @ {fmt_ts(bad[-1].get('updated_at_ms'))}")

        # store 自己的 state（只在收尾后写，正在跑的那轮看不到）与 runs/ 交叉验证
        st = j.get("state") or {}
        if st.get("lastRunAtMs"):
            age_h = (time.time() * 1000 - st["lastRunAtMs"]) / 3600000
            if st.get("lastStatus") not in ("ok", None):
                add("FAIL", "job:state", f"{name} store 里上次结果 {st.get('lastStatus')}：{(st.get('lastError') or '')[:70]}")
            else:
                add("OK", "job:state", f"{name} 上次成功 {age_h:.1f}h 前（runHistory {len(st.get('runHistory') or [])}/20）")

        slots = expected_slots(sch, start, now)
        if slots is None:
            add("WARN", "job:schedule", f"{name} 的表达式 {sched_txt} 超出解析能力，跳过槽位核对")
            continue
        stamps = [(r.get("_start_ms") or r.get("updated_at_ms") or 0) / 1000 for r in mine]
        missing = []
        down = journal_downtime(start_ts) if args.downtime == "journal" else []
        skipped = 0
        down_note = ""
        for slot in slots:
            s = slot.timestamp()
            if any(a <= s <= b for a, b in down):
                skipped += 1
                continue
            if any(s - SLOT_TOL_BEFORE_S <= t <= s + SLOT_TOL_AFTER_S for t in stamps):
                continue
            missing.append(slot)
        if skipped:
            a, b = down[-1]
            down_note = (f"（机器停机跳过 {skipped} 次：{datetime.fromtimestamp(a):%m-%d %H:%M}"
                         f"→{datetime.fromtimestamp(b):%m-%d %H:%M}）")
        total = len(slots) - skipped
        if not total:
            add("OK", "job:slots", f"{name} {sched_txt}，近 {args.hours:g}h 无槽位{down_note}{edit_note}")
        elif missing:
            lvl = "FAIL" if len(missing) > max(1, total // 4) else "WARN"
            shown = ", ".join(m.strftime("%m-%d %H:%M") for m in missing[-4:])
            add(lvl, "job:slots", f"{name} {sched_txt}：应跑 {total} 次、实跑 {total-len(missing)} 次，缺 {len(missing)} 次（{shown}）{down_note}{edit_note}")
        elif total:
            add("OK", "job:slots", f"{name} {sched_txt}：应跑 {total} 次、实跑 {total-len(missing)} 次{down_note}{edit_note}")

        if not mine:
            add("WARN", "job:norecord", f"{name} 在册但从来没有 run 记录")

    # 4) runs 目录无内置轮转，报体积与可清理量（清理工具：tools/cron-runs-prune.py）
    files = [f for f in runs_dir.glob("*.json") if f.is_file()]
    size = sum(f.stat().st_size for f in files)
    cutoff = time.time() * 1000 - 30 * 86400_000
    by_job: dict[str, list[int]] = {}
    for r in runs:
        by_job.setdefault(str(r.get("job_id") or r.get("job_name") or "?"), []).append(
            r.get("updated_at_ms") or 0)
    pruneable = 0
    for ts_list in by_job.values():
        keep = max(ts_list) if ts_list else 0
        pruneable += sum(1 for t in ts_list if t < cutoff and t != keep)
    stamps = [r.get("updated_at_ms") or 0 for r in runs]
    oldest_d = (time.time() * 1000 - min(stamps)) / 86400_000 if any(stamps) else 0
    if len(files) > 500 or size > 5 * 1024 * 1024:
        lvl = "WARN"
    else:
        lvl = "OK"
    add(lvl, "runs:disk",
        f"{len(files)} 个文件 {size/1024:.0f} KiB，最老 {oldest_d:.0f} 天，"
        f">30 天可清理 {pruneable} 个（tools/cron-runs-prune.py --apply）")

    return emit(args)


def emit(args) -> int:
    fails = sum(1 for c in checks if c["level"] == "FAIL")
    warns = sum(1 for c in checks if c["level"] == "WARN")
    if args.json:
        print(json.dumps({
            "ts": datetime.now().strftime("%F %T"),
            "fail": fails, "warn": warns, "checks": checks,
        }, ensure_ascii=False))
        return 1 if fails else 0
    print(f"cron-health {datetime.now():%F %T}  fail={fails} warn={warns}")
    for c in checks:
        print(f"[{c['level']}] {c['name']}: {c['detail']}")
    return 1 if fails else 0


if __name__ == "__main__":
    sys.exit(main())
