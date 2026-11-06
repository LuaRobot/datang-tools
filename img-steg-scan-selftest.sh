#!/bin/sh
# img-steg-scan.py 自测：干净图和大噪点图必须 clean，两种夹带必须 SUSPECT，大比例位替换必须 WORTH_A_LOOK。
set -u
cd "$(dirname "$0")" || exit 1
T=$(mktemp -d)
trap 'rm -rf "$T"' EXIT
PY_BIN=${DATANG_VENV:-$HOME/.nanobot/venv}/bin/python
"$PY_BIN" - "$T" <<'EOF'
import sys
from PIL import Image, PngImagePlugin
d = sys.argv[1]
import math, random
rng0 = random.Random(20261024)
src = Image.new('RGB', (640, 400))
px0 = src.load()
for y in range(400):
    for x in range(640):
        g = 40 + (150 * x) // 640 + (30 * y) // 400
        dd = 25.0 * (math.sin(x / 9.0) + math.sin(y / 13.0) + 0.5 * math.sin((x + y) / 31.0))
        n0 = rng0.randint(-1, 1)
        px0[x, y] = (max(0, min(255, int(g + dd + n0))),
                     max(0, min(255, int(g - 10 + 0.8 * dd + n0))),
                     max(0, min(255, int(g + 12 + 1.2 * dd + n0))))
src.save(f'{d}/clean.png')
open(f'{d}/appended.png','wb').write(open(f'{d}/clean.png','rb').read() + b'PAYLOAD'*50)
info = PngImagePlugin.PngInfo(); info.add_text('Comment','hidden')
src.save(f'{d}/text.png', pnginfo=info)
src.save(f'{d}/plain.jpg', quality=90)
# RS 对照组：同一张图 LSB 顺序嵌入 50%，估算值必须明显抬高
import random
im = src.copy(); px = im.load(); w, h = im.size
n = int(w * h * 0.5); rng = random.Random(7)
i = 0
for y in range(h):
    for x in range(w):
        if i >= n:
            break
        r, g, b = px[x, y]
        px[x, y] = ((r & ~1) | rng.getrandbits(1), g, b)
        i += 1
im.save(f'{d}/lsb50.png')
# 三通道都嵌 90%：给「值得看」第三档用，让均值稳稳过 0.20
im2 = src.copy(); px2 = im2.load(); rng2 = random.Random(11)
n2 = int(w * h * 0.9)
for y in range(h):
    for x in range(w):
        if (y * w + x) >= n2:
            break
        r, g, b = px2[x, y]
        px2[x, y] = ((r & ~1) | rng2.getrandbits(1),
                     (g & ~1) | rng2.getrandbits(1),
                     (b & ~1) | rng2.getrandbits(1))
im2.save(f'{d}/lsb-all90.png')
# 重度噪点负样本：加高斯噪点后 LSB 天然随机，必须不被当成嵌入
import numpy as np
arr = np.asarray(src).astype(np.float32)
noise = np.random.default_rng(3).normal(0, 8, arr.shape)
Image.fromarray(np.clip(arr + noise, 0, 255).astype(np.uint8)).save(f'{d}/noisy.png')
EOF
fail=0
expect() { # file want_verdict
  out=$(./img-steg-scan.py --json "$1" 2>/dev/null | "$PY_BIN" -c 'import json,sys;print(json.load(sys.stdin)[0]["verdict"])')
  if [ "$out" = "$2" ]; then echo "ok   $(basename "$1") -> $out"
  else echo "FAIL $(basename "$1") -> $out (want $2)"; fail=1; fi
}
# RS 估算值：干净图应 ≤0.10，嵌入 50% 应落在 0.35–0.65
expect_rs() { # file lo hi
  out=$(./img-steg-scan.py --json "$1" 2>/dev/null | "$PY_BIN" -c 'import json,sys;print(json.load(sys.stdin)[0]["rs"]["planes"]["R"]["p"])')
  ok=$("$PY_BIN" -c "import sys;p=float(sys.argv[1]);lo=float(sys.argv[2]);hi=float(sys.argv[3]);print(1 if lo<=p<=hi else 0)" "$out" "$2" "$3")
  if [ "$ok" = 1 ]; then echo "ok   RS $(basename "$1") -> $out ∈ [$2,$3]"
  else echo "FAIL RS $(basename "$1") -> $out 不在 [$2,$3]"; fail=1; fi
}
expect "$T/clean.png" clean
expect "$T/plain.jpg" clean
expect "$T/appended.png" SUSPECT
expect "$T/text.png" SUSPECT
expect "$T/lsb-all90.png" WORTH_A_LOOK
expect "$T/noisy.png" clean
expect_rs "$T/clean.png" 0.0 0.10
expect_rs "$T/lsb50.png" 0.35 0.65
echo "---"
[ "$fail" = 0 ] && echo "自测通过" || echo "自测失败"
exit $fail
