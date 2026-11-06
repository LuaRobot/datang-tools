#!/bin/bash
# notes-lint-selftest.sh —— 造两个假工作区，断言「该报的报、该放过的放过」。
set -u
TOOL="$(dirname "$(readlink -f "$0")")/notes-lint.py"
T=$(mktemp -d); trap 'rm -rf "$T"' EXIT
rc=0
ck() { if [ "$1" = "$2" ]; then echo "ok   $3"; else echo "FAIL $3（得到 $1 期望 $2）"; rc=1; fi }

# ---- 坏工作区：2 条真断链 + 1 条已有路径 + 1 条散文片段
mkdir -p "$T/bad/notes/nightly" "$T/bad/notes/qq"
cat > "$T/bad/MEMORY.md" <<'MD'
- 笔记 `notes/nightly/gone.md` 第 3 节。
- 见 `notes/qq/README.md`。
- 笔记 `notes/nightly/2026-01-01.md` 第 9 节。
MD
printf '## 1. 有编号的一节\n' > "$T/bad/notes/nightly/2026-01-01.md"
echo hi > "$T/bad/notes/qq/README.md"
out=$("$TOOL" --ws "$T/bad" --scope prompts --json); code=$?
ck "$(echo "$out" | python3 -c 'import json,sys;d=json.load(sys.stdin);print(d["fail"])')" 2 "坏工作区抓到 2 条 FAIL"
ck "$code" 1 "坏工作区退出码 1"

# ---- 干净工作区：同一批指针都存在且节号对得上
cp -r "$T/bad" "$T/ok"
printf '## 3. 存在的一节\n' > "$T/ok/notes/nightly/gone.md"
printf '## 9. 第九节\n' >> "$T/ok/notes/nightly/2026-01-01.md"
out=$("$TOOL" --ws "$T/ok" --scope prompts --json); code=$?
ck "$(echo "$out" | python3 -c 'import json,sys;d=json.load(sys.stdin);print(d["fail"])')" 0 "干净工作区 0 FAIL"
ck "$code" 0 "干净工作区退出码 0"

# ---- 散文片段不该被当成断链
mkdir -p "$T/prose/notes"
printf '同 session 有 cron/trigger 主动轮在跑。\n' > "$T/prose/notes/a.md"
out=$("$TOOL" --ws "$T/prose" --scope notes --json)
ck "$(echo "$out" | python3 -c 'import json,sys;d=json.load(sys.stdin);print(len(d["problems"]))')" 0 "散文片段不误报"

# ---- THREADS 式日期块：条目自带日期优先，无日期才用块标题
mkdir -p "$T/dated/notes/nightly"
printf '## 3. 三\n' > "$T/dated/notes/nightly/2026-02-01.md"
printf '## 7. 七\n' > "$T/dated/notes/nightly/2026-02-02.md"
cat > "$T/dated/notes/nightly/THREADS.md" <<'MD'
## 2026-02-01 10:00 新增
- [x] 甲（当天笔记第 3 节，lab `labs/x-2026-01-20/`）。
- [x] 乙（2026-02-02 09:00 补：第 7 节）。
- [x] 丙（当天笔记第 9 节）。
MD
out=$("$TOOL" --ws "$T/dated" --scope notes --json)
ck "$(echo "$out" | python3 -c 'import json,sys;d=json.load(sys.stdin);print(len([p for p in d["problems"] if p["kind"]=="section"]))')" 1 "日期块推断：前两条挂对、丙那条报缺"

exit $rc
