#!/usr/bin/env bash
# logdigest.sh —— 把一大坨日志压成「模板 × 次数」的摘要，省 token。
#
# 用途：journalctl / 应用日志动辄几百 KB，直接读进上下文太贵。
#       本脚本剥掉时间戳、pid、路径数字等易变部分，把同类行归成一个模板，
#       按出现次数排序输出，并给出压缩比。
#
# 用法：
#   logdigest.sh /var/log/foo.log            # 读文件
#   journalctl -u x --since -1h | logdigest.sh -   # 读 stdin
#   logdigest.sh - --top 30 --level 'ERROR|WARN' --examples 2
#   logdigest.sh - --raw                     # 不做模板归一，只去重计数
#
# 选项：
#   --top N        显示前 N 个模板（默认 15）
#   --level RE     只看匹配 RE 的行（在管道前过滤，如 'ERROR|WARN|Traceback'）
#   --examples N   每个模板下附 N 条原始样例（默认 0）
#   --width N      模板截断宽度（默认 150）
#   --raw          不做归一化，只统计完全相同的行
#   --no-header    不打印统计头
#   -h|--help
#
# 退出码：0 正常；1 无输入；2 参数错误。
set -uo pipefail

TOP=15
LEVEL=""
EXAMPLES=0
WIDTH=150
RAW=0
HEADER=1
SRC=""

die() { echo "logdigest: $*" >&2; exit 2; }

while [ $# -gt 0 ]; do
  case "$1" in
    --top)      TOP="${2:-}"; [ -n "$TOP" ] || die "--top 需要数字"; shift 2 ;;
    --level)    LEVEL="${2:-}"; shift 2 ;;
    --examples) EXAMPLES="${2:-0}"; shift 2 ;;
    --width)    WIDTH="${2:-150}"; shift 2 ;;
    --raw)      RAW=1; shift ;;
    --no-header) HEADER=0; shift ;;
    -h|--help)  sed -n '2,30p' "$0" | sed 's/^# \{0,1\}//'; exit 0 ;;
    -)          SRC="-"; shift ;;
    -*)         die "未知选项 $1" ;;
    *)          SRC="$1"; shift ;;
  esac
done

case "$TOP" in ''|*[!0-9]*) die "--top 需正整数" ;; esac
case "$EXAMPLES" in ''|*[!0-9]*) die "--examples 需非负整数" ;; esac

if [ -z "$SRC" ]; then
  if [ -t 0 ]; then die "需要文件参数或从 stdin 喂入（用 - 显式表示 stdin）"; fi
  SRC="-"
fi
[ "$SRC" = "-" ] || [ -r "$SRC" ] || die "读不到文件 $SRC"

TMPD="$(mktemp -d)"
trap 'rm -rf "$TMPD"' EXIT

if [ -n "$LEVEL" ]; then
  if [ "$SRC" = "-" ]; then cat; else cat -- "$SRC"; fi | grep -E -- "$LEVEL" > "$TMPD/in" || true
else
  if [ "$SRC" = "-" ]; then cat; else cat -- "$SRC"; fi > "$TMPD/in"
fi

TOTAL=$(wc -l < "$TMPD/in" | tr -d ' ')
BYTES=$(wc -c < "$TMPD/in" | tr -d ' ')
if [ "$TOTAL" -eq 0 ]; then echo "logdigest: 无匹配行" >&2; exit 1; fi

# --- 归一化：把易变部分换成占位符，得到「模板」 ---
awk -v raw="$RAW" '
function strip_sysd(s) {
  # journalctl short / short-iso / cat 前缀
  sub(/^[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9:.+:-]+ [^ ]+ [^ \[]+\[[0-9]+\]: /, "", s)
  sub(/^[A-Z][a-z]{2} [ 0-9][0-9] [0-9:]{8} [^ ]+ [^ \[]+\[[0-9]+\]: /, "", s)
  return s
}
function normalize(s,   t) {
  if (raw) return s
  s = strip_sysd(s)
  # 应用内时间戳 2026-10-21 18:25:00 / 18:25:00.123
  gsub(/[0-9]{4}-[0-9]{2}-[0-9]{2}[ T][0-9]{2}:[0-9]{2}:[0-9]{2}(\.[0-9]+)?/, "<ts>", s)
  gsub(/[0-9]{2}:[0-9]{2}:[0-9]{2}(\.[0-9]+)?/, "<ts>", s)
  # uuid / 长 hex / IP:port / 路径数字
  gsub(/[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}/, "<uuid>", s)
  gsub(/0x[0-9a-fA-F]+/, "<hex>", s)
  gsub(/[0-9]{1,3}(\.[0-9]{1,3}){3}(:[0-9]+)?/, "<ip>", s)
  # 工具调用的大块 JSON 参数：保留工具名，掐掉载荷
  if (match(s, /Tool call: [A-Za-z0-9_.]+\(/)) {
    head = substr(s, 1, RSTART + RLENGTH)
    s = head "\342\200\246)"
  }
  gsub(/[0-9]+/, "#", s)          # 剩余数字
  gsub(/["\x27][^"\x27]{40,}["\x27]/, "<str>", s)  # 长引号串
  gsub(/[ \t]+/, " ", s)
  sub(/^ /, "", s); sub(/ $/, "", s)
  return s
}
{
  t = normalize($0)
  cnt[t]++
  if (!(t in first)) {
    ex = $0; gsub(/\t/, " ", ex)
    first[t] = ex
  }
}
END {
  for (t in cnt) printf "%d\t%s\t%s\n", cnt[t], t, first[t]
}
' "$TMPD/in" > "$TMPD/counts"

UNIQ=$(wc -l < "$TMPD/counts" | tr -d ' ')
SORTED="$TMPD/sorted"
LC_ALL=C sort -t "$(printf '\t')" -k1,1rn -k2,2 "$TMPD/counts" | head -n "$TOP" > "$SORTED"

if [ "$HEADER" -eq 1 ]; then
  echo "[logdigest] 行 $TOTAL / 字节 $BYTES / 去重后 $UNIQ 个模板 / 显示前 $(wc -l < "$SORTED" | tr -d ' ') 个"
  echo
fi

while IFS="$(printf '\t')" read -r n tmpl sample; do
  short="$tmpl"
  if [ "${#short}" -gt "$WIDTH" ]; then short="${short:0:$WIDTH}…"; fi
  printf '%6s×  %s\n' "$n" "$short"
  if [ "$EXAMPLES" -gt 0 ] && [ -n "${sample:-}" ]; then
    printf '        │ %s\n' "${sample:0:400}"
  fi
done < "$SORTED"

exit 0
