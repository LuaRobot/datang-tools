#!/usr/bin/env bash
# cron-runs-prune.py 自测：合成语料，断言 4 类保留 + 1 类删除真的按预期走。
set -u
export PATH=/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin
TOOL="$(cd "$(dirname "$0")" && pwd)/cron-runs-prune.py"
TMP="$(mktemp -d)"
trap 'rm -rf "$TMP"' EXIT
fail=0
ok()   { echo "  ok   $*"; }
bad()  { echo "  FAIL $*"; fail=1; }

mk() { # mk <file> <job_id> <status> <age_days>
  local f="$TMP/$1" ts=$(( ($(date +%s) - $4 * 86400) * 1000 ))
  cat > "$f" <<EOF
{"job_id":"$2","job_name":"job-$2","status":"$3","updated_at_ms":$ts}
EOF
}

# --- 60 天前的旧记录：应删
mk old_dead_job.json       dead0001 ok      60
mk old_dead_job2.json      dead0001 ok      50
# --- 40 天前，但是该 job 唯一一条：应保留（keep-last）
mk only_old.json           solo0001 ok      40
# --- 40 天前 + 有更新的同 job：应删
mk old_with_newer.json     live0001 ok      40
mk newer.json              live0001 ok      2
# --- 40 天前但 status=queued：不删
mk stuck_queued.json       live0002 queued  40
# --- 未超期：不删
mk fresh.json              live0003 ok      1
# --- 坏 json：不删，报出来
echo '{not json' > "$TMP/broken.json"

echo "== dry-run（默认不改盘）"
out="$(python3 "$TOOL" --runs "$TMP" --days 30 --json)"
cnt=$(printf '%s' "$out" | python3 -c 'import json,sys;d=json.load(sys.stdin);print(d["delete_count"])')
[ "$cnt" = 2 ] && ok "dry-run 应删 2 个（got $cnt）" || bad "dry-run 应删 2 个（got $cnt）"
n=$(ls "$TMP" | wc -l)
[ "$n" = 8 ] && ok "dry-run 没动盘（8 个文件）" || bad "dry-run 动了盘（$n 个文件）"
printf '%s' "$out" | grep -q 'broken.json' && ok "坏 json 报到 bad_json" || bad "坏 json 没报"

echo "== --apply"
python3 "$TOOL" --runs "$TMP" --days 30 --apply --json >/dev/null
for keep in only_old.json newer.json stuck_queued.json fresh.json broken.json; do
  [ -f "$TMP/$keep" ] && ok "保留 $keep" || bad "误删 $keep"
done
for gone in old_dead_job.json old_with_newer.json; do
  [ ! -f "$TMP/$gone" ] && ok "删掉 $gone" || bad "该删没删：$gone"
done
# dead0001 里 50 天那条是最近一条，被 keep-last 保住；60 天那条才该删
[ -f "$TMP/old_dead_job2.json" ] && ok "保留 old_dead_job2.json（该 job 最近一条）" || bad "误删 keep-last"
[ ! -f "$TMP/old_dead_job.json" ] && ok "删掉 old_dead_job.json（同 job 较旧那条）" || bad "该删没删（较旧那条）"

echo "== 幂等 / 空目录"
python3 "$TOOL" --runs "$TMP" --days 30 --apply --json | grep -q '"delete_count": 0' \
  && ok "二次运行删 0" || bad "二次运行不幂等"
python3 "$TOOL" --runs "$TMP/nope" >/dev/null 2>&1; [ $? -eq 2 ] && ok "目录不存在 rc=2" || bad "目录不存在 rc 不为 2"

echo
[ "$fail" = 0 ] && echo "SELFTEST PASS" || echo "SELFTEST FAIL"
exit "$fail"
