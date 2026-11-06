#!/usr/bin/env bash
# secret-scan.sh —— push 前扫一遍「有没有把凭据带进仓库」。
#
# 分两档：
#   FAIL  高置信模式（真实凭据才会长得这样），命中就停下来别推
#   WARN  上下文关键词（apiKey/token/password 之类出现在赋值或 JSON 里），人工看一眼
#
# 用法：
#   ./secret-scan.sh            # 扫当前目录（不含 .git）
#   ./secret-scan.sh path...    # 扫指定文件/目录
#   ./secret-scan.sh --quiet    # 只打印 FAIL
# 退出码：有 FAIL 时 1，否则 0。
set -uo pipefail

quiet=0
args=()
for a in "$@"; do
  case "$a" in
    --quiet|-q) quiet=1 ;;
    *) args+=("$a") ;;
  esac
done
[ ${#args[@]} -eq 0 ] && args=(.)

fail=0
warn=0

# 高置信：这些形态基本只可能是真凭据
HIGH=(
  'ghp_[A-Za-z0-9]{20,}'                       # GitHub PAT (classic)
  'github_pat_[A-Za-z0-9_]{30,}'               # GitHub PAT (fine-grained)
  'gh[osur]_[A-Za-z0-9]{30,}'                  # GitHub OAuth/app/user/refresh
  'sk-[A-Za-z0-9_-]{20,}'                      # OpenAI 风格
  'AKIA[0-9A-Z]{16}'                           # AWS access key id
  'xox[baprs]-[A-Za-z0-9-]{10,}'               # Slack
  '-----BEGIN [A-Z ]*PRIVATE KEY-----'         # 私钥
  'eyJ[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}'   # JWT
  'ctk_[A-Za-z0-9]{16,}'                       # agently 确认令牌
  'rkey=[A-Za-z0-9]{20,}'                      # 腾讯图床临时 key
  'i:(api[_-]?key|secret|password|passwd|token)["'"'"']?\s*[:=]\s*["'"'"']?[A-Za-z0-9/+_.-]{16,}'
)
# 中置信：出现即提示，但很多是文档里的字段名
LOW=(
  'i:(api[_-]?key|apikey|secret|password|passwd|passphrase|cookie|token)'
  'i:(Authorization:\s*Bearer)'
  'i:(ssh-rsa AAAA|ssh-ed25519 AAAA)'
)

# grep -E 不认内联 (?i)，大小写不敏感靠前缀 "i:" 标记
scan() {
  local pat="$1" flags=-E out
  case "$pat" in i:*) flags=-iE; pat="${pat#i:}" ;; esac
  out=$(grep -rIn $flags --binary-files=without-match \
        --exclude-dir=.git --exclude-dir=__pycache__ \
        --exclude='secret-scan.sh' -e "$pat" "${args[@]}" 2>/dev/null)
  printf '%s' "$out"
}

for p in "${HIGH[@]}"; do
  hits=$(scan "$p")
  if [ -n "$hits" ]; then
    fail=$((fail + $(printf '%s\n' "$hits" | wc -l)))
    echo "FAIL  /${p#i:}/"
    printf '%s\n' "$hits" | sed 's/^/      /' | cut -c1-200
  fi
done

if [ "$quiet" -eq 0 ]; then
  # 低置信只报文件名与条数，不刷屏整行
  for p in "${LOW[@]}"; do
    hits=$(scan "$p")
    [ -z "$hits" ] && continue
    n=$(printf '%s\n' "$hits" | wc -l)
    warn=$((warn + n))
    echo "WARN  /$p/ → $n 处，涉及文件："
    printf '%s\n' "$hits" | cut -d: -f1 | sort -u | sed 's/^/      /'
  done
fi

echo
if [ "$fail" -gt 0 ]; then
  echo "结果：FAIL $fail 条 / WARN $warn 处 —— 先处理 FAIL 再推。"
  exit 1
fi
echo "结果：无高置信凭据（WARN $warn 处，人工确认后即可）。"
