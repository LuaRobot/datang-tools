#!/bin/sh
# mem-dup.py 自测：合成语料验四个判据真会响、也真会安静。
#   1) 逐字重复 → Jaccard 接近 1，必须抓到
#   2) 改写重复（换词序、删一半虚词）→ 仍要 ≥0.45
#   3) 同主题但不同事实 → 不许误报
#   4) 长条目：带「笔记第 N 节」指针的不进可外链，不带的要进
#   5) 一个 bullet 块切 3 条
set -u
cd "$(dirname "$0")" || exit 1
T=$(mktemp -d)
trap 'rm -rf "$T"' EXIT
mkdir -p "$T/memory"

cat > "$T/AGENTS.md" <<'EOF'
# 规则

- 改关键文件先备份：`config.json`、unit 文件、sudoers 条目，先 `cp -a` 到备份目录再用。
- 跑长命令用 `yield_time_ms` 加轮询日志，不要靠管道接力，否则父 shell 回收会让命令卡死。
EOF

cat > "$T/SOUL.md" <<'EOF'
# Soul

- 改关键文件先备份：`config.json`、unit 文件、sudoers 条目，先 `cp -a` 到备份目录再用。
- 改关键文件前先备份 config.json 和 unit 文件，用 cp -a 拷到备份目录，然后再动手。
- gateway 运行时用的是 python 3.13，venv 在 ~/.nanobot/venv，装包要走 venv 里的 pip3。
EOF

cat > "$T/USER.md" <<'EOF'
# User

一句占位说明文字，用来占住条目计数，长度不够进候选。
EOF

{ printf '# Memory\n\n'
  printf -- '- %s\n' "$(python3 -c "print('长条目正文' * 90)")"
  printf -- '- %s\n' "$(python3 -c "print('带指针的长条目' * 86)")"'详见 `notes/nightly/2026-10-01.md` 笔记第 3 节。'
  printf -- '- 第一条\n- 第二条\n- 第三条\n'
} > "$T/memory/MEMORY.md"

fail=0
chk() { # label condition
  if [ "$2" = "1" ]; then echo "ok   $1"; else echo "FAIL $1"; fail=1; fi
}

J=$("$PWD/mem-dup.py" --root "$T" --json --show-text --threshold 0.45 --min-len 400)
run() { printf '%s' "$J" | python3 -c "$1"; }

chk "逐字重复被抓到（score ≥ 0.9）" "$(run '
import json,sys
d=json.load(sys.stdin)
print(1 if any(x["score"]>=0.9 and x["a"]["file"].endswith("AGENTS.md") for x in d["dupes"]) else 0)')"

chk "改写重复被抓到（AGENTS↔SOUL ≥ 0.45）" "$(run '
import json,sys
d=json.load(sys.stdin)
print(1 if any({x["a"]["file"],x["b"]["file"]}=={"'$T'/AGENTS.md","'$T'/SOUL.md"} for x in d["dupes"]) else 0)')"

chk "同主题不同事实不误报" "$(run '
import json,sys
d=json.load(sys.stdin)
bad=[x for x in d["dupes"] if any("python 3.13" in e["text"] for e in (x["a"],x["b"]))]
print(0 if bad else 1)')"

chk "带笔记指针的长条目不进候选" "$(run '
import json,sys
d=json.load(sys.stdin)
print(0 if any("带指针" in e["text"] for e in d["external"]) else 1)')"

chk "无指针长条目进候选（≥1 条）" "$(run '
import json,sys
d=json.load(sys.stdin)
print(1 if any("长条目正文" in e["text"] for e in d["external"]) else 0)')"

chk "bullet 块切成 3 条独立条目" "$(run '
import json,sys
d=json.load(sys.stdin)
print(1 if d["files"]["'$T'/memory/MEMORY.md"]["entries"]>=4 else 0)')"

# 负向：阈值从 0.45 抬到 0.99 后，AGENTS↔SOUL 只剩逐字那条（证明阈值真在起作用）
J=$("$PWD/mem-dup.py" --root "$T" --json --show-text --threshold 0.99 --min-len 400)
chk "阈值调高后改写重复消失、只留逐字重复" "$(printf '%s' "$J" | python3 -c '
import json,sys
d=json.load(sys.stdin)
n=sum(1 for x in d["dupes"] if {x["a"]["file"],x["b"]["file"]}=={"'$T'/AGENTS.md","'$T'/SOUL.md"})
print(1 if n==1 else 0)')"

if [ "$fail" = 0 ]; then echo "全部通过 (6 + 1 负向)"; else echo "有失败项"; fi
exit "$fail"
