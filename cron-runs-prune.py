#!/usr/bin/env python3
"""cron/runs/ 轮转清理。

背景：nanobot 的 `utils/run_records.py` 只写不删，一个 run 一个 json 文件，
一年下来是几千个小文件（见 notes/nightly/2026-10-22.md 第 11 节）。

规则（保守）：
  - 只删 cron/runs/ 下的 *.json，绝不碰 jobs.json、action.jsonl、runs/ 目录本身；
  - 只删 updated_at_ms 早于 --days 天（默认 30）的文件，读不出时间的按 mtime；
  - 每个 job（job_id，缺则 job_name）**最近 --keep-last 条（默认 1）永远保留**；
  - status 为 queued / running 的一律不删（那是巡检的证据，也可能是僵尸）；
  - 解析失败的 json 不删，只报。

默认 dry-run，只打印会删哪些；加 --apply 才真删。

用法：
  cron-runs-prune.py                 # 看看能删多少
  cron-runs-prune.py --days 30 --apply
  cron-runs-prune.py --json
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import time
from collections import defaultdict
from pathlib import Path

HOME = Path.home()
DEFAULT_RUNS = HOME / ".nanobot/workspace/cron/runs"
KEEP_STATUS = {"queued", "running"}


def run_ts(d: dict, f: Path) -> int:
    ts = d.get("updated_at_ms")
    if isinstance(ts, (int, float)) and ts > 0:
        return int(ts)
    return int(f.stat().st_mtime * 1000)


def job_key(d: dict) -> str:
    return str(d.get("job_id") or d.get("job_name") or "?")


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--runs", default=str(DEFAULT_RUNS))
    ap.add_argument("--days", type=int, default=30)
    ap.add_argument("--keep-last", type=int, default=1,
                    help="每个 job 无条件保留的最近条数（默认 1）")
    ap.add_argument("--apply", action="store_true", help="真删（默认只报告）")
    ap.add_argument("--json", action="store_true")
    args = ap.parse_args()

    runs_dir = Path(args.runs)
    if not runs_dir.is_dir():
        print(f"runs 目录不存在：{runs_dir}", file=sys.stderr)
        return 2

    now_ms = int(time.time() * 1000)
    cutoff_ms = now_ms - args.days * 86400_000

    rows, bad = [], []
    for f in sorted(runs_dir.glob("*.json")):
        if not f.is_file():
            continue
        try:
            d = json.loads(f.read_text(encoding="utf-8"))
            if not isinstance(d, dict):
                raise ValueError("not an object")
        except Exception:
            bad.append(f.name)
            continue
        rows.append({"file": f, "ts": run_ts(d, f), "job": job_key(d),
                     "status": str(d.get("status") or ""), "name": d.get("job_name") or ""})

    per_job = defaultdict(list)
    for r in rows:
        per_job[r["job"]].append(r)
    spotlight = set()
    for lst in per_job.values():
        for r in sorted(lst, key=lambda x: x["ts"], reverse=True)[: max(0, args.keep_last)]:
            spotlight.add(r["file"].name)

    keep_newest, keep_fresh, keep_status = [], [], []
    doomed = []
    for r in sorted(rows, key=lambda x: x["ts"]):
        if r["ts"] >= cutoff_ms:
            keep_fresh.append(r)
        elif r["status"] in KEEP_STATUS:
            keep_status.append(r)
        elif r["file"].name in spotlight:
            keep_newest.append(r)
        else:
            doomed.append(r)

    total_size = sum(r["file"].stat().st_size for r in rows if r["file"].exists()) + \
        sum((runs_dir / b).stat().st_size for b in bad if (runs_dir / b).exists())

    freed = 0
    errors = []
    for r in doomed:
        try:
            size = r["file"].stat().st_size
            if args.apply:
                r["file"].unlink()
            freed += size
        except Exception as e:  # noqa: BLE001
            errors.append(f"{r['file'].name}: {e}")

    if args.json:
        print(json.dumps({
            "dry_run": not args.apply,
            "runs_dir": str(runs_dir),
            "days": args.days,
            "files_total": len(rows) + len(bad),
            "size_total_kib": round(total_size / 1024, 1),
            "delete_count": len(doomed),
            "freed_kib": round(freed / 1024, 1),
            "kept_fresh": len(keep_fresh),
            "kept_newest": len(keep_newest),
            "kept_status": len(keep_status),
            "deleted": [r["file"].name for r in doomed] if args.apply else [],
            "would_delete": [r["file"].name for r in doomed],
            "bad_json": bad,
            "errors": errors,
        }, ensure_ascii=False, indent=2))
    else:
        mode = "已删除" if args.apply else "待删除（dry-run）"
        print(f"cron/runs: {len(rows) + len(bad)} 个文件 / {total_size/1024:.1f} KiB，"
              f"阈值 {args.days} 天")
        print(f"  {mode}: {len(doomed)} 个 / {freed/1024:.1f} KiB")
        print(f"  保留: 未超期 {len(keep_fresh)}、每 job 最近 {len(keep_newest)}、"
              f"queued/running {len(keep_status)}")
        for r in doomed[:20]:
            print(f"    - {r['file'].name}  {r['name']} {r['status']}")
        if len(doomed) > 20:
            print(f"    …另 {len(doomed)-20} 个")
        if bad:
            print(f"  解析失败（不删）: {', '.join(bad)}")
        for e in errors:
            print(f"  删除失败: {e}")

    return 1 if errors else 0


if __name__ == "__main__":
    sys.exit(main())
