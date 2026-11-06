#!/usr/bin/env bash
# cron-health-selftest.sh —— 用合成语料验证 cron-health.py 的每个判定分支真的会响
# 断言：僵尸 queued、非 ok 状态、槽位漏跑、绑定残缺 → 必须 FAIL；
#      禁用 job、孤儿记录 → 必须 WARN；全部修好 → 必须 exit 0。
set -uo pipefail
export PATH=${PATH:-/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin}
T="$(dirname "$(readlink -f "$0")")/cron-health.py"
PY="${DATANG_VENV:-$HOME/.nanobot/venv}/bin/python"
[ -x "$PY" ] || PY=python3
d=$(mktemp -d /tmp/cronhealth-XXXXXX); trap 'rm -rf "$d"' EXIT
mkdir -p "$d/runs"

$PY - "$d" <<'PY'
import json, sys, time, os
d = sys.argv[1]; now = time.time()
def job(id_, name, enabled, expr, to=None, deliver=False):
    return {"id": id_, "name": name, "enabled": enabled,
            "schedule": {"kind": "cron", "expr": expr, "tz": "Asia/Shanghai"},
            "payload": {"kind": "agent_turn", "deliver": deliver, "channel": to, "to": to,
                        "channelMeta": {}, "sessionKey": ("websocket:datang-self" if not to else None),
                        "originChannel": ("websocket" if not to else None),
                        "originChatId": ("datang-self" if not to else None)},
            "createdAtMs": int((now - 4 * 86400) * 1000)}
store = {"version": 1, "jobs": [
    job("aaaaaaaa", "self-directed-2h", True, "25 */2 * * *"),
    job("bbbbbbbb", "paused-job", False, "0 9 * * *"),
    job("cccccccc", "leaky-deliver", True, "0 10 * * *", to="telegram", deliver=True),
]}
json.dump(store, open(os.path.join(d, "jobs.json"), "w"))
def rec(name, jid, status, ago_s):
    ts = int((now - ago_s) * 1000)
    json.dump({"job_id": jid, "job_name": name, "status": status, "updated_at_ms": ts,
               "session_key": "websocket:datang-self", "response": "x"},
              open(os.path.join(d, "runs", "%s_%d.json" % (jid, ts)), "w"))
rec("self-directed-2h", "aaaaaaaa", "ok", 3600)       # 只盖住最近一个槽位
rec("self-directed-2h", "aaaaaaaa", "error", 7200)    # 非 ok
rec("self-directed-2h", "aaaaaaaa", "queued", 4*3600) # 僵尸 queued
rec("ghost-job", "dddddddd", "ok", 600)               # 孤儿记录
PY

out=$($PY "$T" --store "$d/jobs.json" --runs "$d/runs" --usage "$d/none.sqlite3" --gateway-start-ms 0 --downtime none 2>&1); rc=$?
fail=0
want_fail=(runs:stuck job:status job:slots job:binding)
want_warn=(job:disabled runs:orphan)
for k in "${want_fail[@]}"; do grep -q "^\[FAIL\] $k" <<<"$out" || { echo "MISSING FAIL: $k"; fail=1; }; done
for k in "${want_warn[@]}"; do grep -q "^\[WARN\] $k" <<<"$out" || { echo "MISSING WARN: $k"; fail=1; }; done
[ "$rc" -eq 1 ] || { echo "期望 exit 1，实际 $rc"; fail=1; }

# 重启掐断：gateway 启动晚于那条 queued 记录 → 必须是 WARN runs:interrupted，不能算僵尸 FAIL
out3=$($PY "$T" --store "$d/jobs.json" --runs "$d/runs" --usage "$d/none.sqlite3" \
       --gateway-start-ms $(( ($(date +%s) - 3600) * 1000 )) --downtime none 2>&1)
grep -q '^\[WARN\] runs:interrupted' <<<"$out3" || { echo 'MISSING WARN: runs:interrupted'; fail=1; }
grep -q '^\[FAIL\] runs:stuck' <<<"$out3" && { echo '重启掐断仍被当僵尸 FAIL'; fail=1; }

# 干净语料必须全绿（只有孤儿/无记录的 WARN 时也算绿：exit 0）
d2=$(mktemp -d /tmp/cronhealth-clean-XXXXXX); mkdir -p "$d2/runs"
$PY - "$d2" <<'PY'
import json, sys, time, os
d = sys.argv[1]; now = time.time()
j = {"id": "aaaaaaaa", "name": "self-directed-2h", "enabled": True,
     "schedule": {"kind": "cron", "expr": "25 */2 * * *", "tz": "Asia/Shanghai"},
     "payload": {"kind": "agent_turn", "deliver": False, "channelMeta": {},
                 "sessionKey": "websocket:datang-self", "originChannel": "websocket",
                 "originChatId": "datang-self"},
     "createdAtMs": int((now - 4 * 86400) * 1000), "updatedAtMs": int((now - 4 * 86400) * 1000)}
json.dump({"version": 1, "jobs": [j]}, open(os.path.join(d, "jobs.json"), "w"))
# 每 2 小时一个槽位（偶数小时 :25）。run 时间必须对齐真实槽位相位，
# 否则在「离 :25 超过容差」的时刻跑自测会假报漏跑。
import datetime
base = datetime.datetime.now().replace(minute=25, second=0, microsecond=0)
while base.timestamp() > now or base.hour % 2:
    base -= datetime.timedelta(hours=1)
for i in range(13):
    ts = int((base - datetime.timedelta(hours=2 * i)).timestamp() * 1000)
    json.dump({"job_id": "aaaaaaaa", "job_name": "self-directed-2h", "status": "ok",
               "updated_at_ms": ts, "response": "x"},
              open(os.path.join(d, "runs", "r%d.json" % i), "w"))
PY
out2=$($PY "$T" --store "$d2/jobs.json" --runs "$d2/runs" --usage "$d2/none.sqlite3" --gateway-start-ms 0 --downtime none 2>&1); rc2=$?
rm -rf "$d2"
[ "$rc2" -eq 0 ] || { echo "干净语料应 exit 0，实际 $rc2"; echo "$out2"; fail=1; }

if [ "$fail" -eq 0 ]; then echo "selftest OK（4 FAIL + 2 WARN 分支全响，重启掐断判 WARN，干净语料 exit 0）"; else echo "selftest FAILED"; fi
exit "$fail"
