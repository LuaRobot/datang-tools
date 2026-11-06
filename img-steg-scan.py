#!/usr/bin/env python3
"""img-steg-scan.py —— 图片藏信息粗筛（容器痕迹 + LSB 统计）

用法:
  ./img-steg-scan.py 图1.png 图2.jpg ...
  ./img-steg-scan.py --json IMG...
  ./img-steg-scan.py --embed-test OUT.png    # 生成对照组：原图 vs LSB 嵌入图

看三件事:
  1) 容器痕迹：IEND/EOI 之后的多余字节、PNG 里 tEXt/zTXt/iTXt/eXIf 块、JPEG 的 APPn/COM 段。
     这是「低技术但真常见」的藏法（cat secret >> a.png），一查一个准。
  2) LSB 卡方（Westfeld-Pfitzmann）：把直方图按 (2i, 2i+1) 配对，自然图两两不等，
     做过 LSB 替换后两两被抹平 → p 值接近 1。
  3) LSB 平面熵 + 相邻像素 LSB 相同率：嵌入后趋近 1.0 与 0.5。
  4) RS 分析（Fridrich 2001）：按 4 像素组算「翻转后判别函数变大/变小」的比例差 d=R-S，
     嵌入让 d 线性趋向 0，于是 p = 1 - d/d_ref。d_ref 同时取「JPEG q90 往返后重算的 d」与
     经典「负掩码 d」，两个估计取小的（宁可漏不误报）。实测：平滑照片类图 ±0.02，
     截图/线稿类图会偏低，0.1 的嵌入率基本测不出。见笔记第 47 节。

判定分三档：
  SUSPECT       容器层面抓到夹带（硬证据）
  WORTH_A_LOOK  RS 均值 ≥0.10 且两个参考不打架、容器无痕 —— 「值得人工看一眼」，不是证据
  clean         其余

WORTH_A_LOOK 的阈值是标定出来的，不是拍的（笔记第 56 节）：18 张真照片 + 壁纸 + 截图 +
线稿 + 纯渐变当负样本，干净图 RS 均值最高 0.017；同一批图顺序 LSB 替换 0.5 时 16/20 命中、
0.9 时 18/20、0.3 时 14/20、0.1 时 0/20。阈值 0.10 在负样本上 0/20 误报，1600×1200 复核同样
0 误报（干净 ≤0.009、嵌入 0.5 为 0.23–0.37）。

但这套规则在**重度噪点图**上会大面积假阳（笔记第 57 节）：人为加高斯噪点 σ≥6 后，
17 张里 11 张被当成「像嵌入」，其中一张 RS 均值 1.000、两个参考完全一致。原因是噪点把 LSB
天然随机化了，统计上跟 LSB 替换一模一样 —— 这是信息论上的死路，不是参数没调好。
所以加噪声闸门（Immerkær 估计 >3.0 就不出第三档，只输出 `hint_suppressed` 说明原因）。
代价：纹理密的图（树林、褪片、高 ISO 夜景）不会给提示，宁漏不误。

注意: 只能查**位替换**类（LSB 替换/顺序嵌入）。DCT 域、调色板顺序、生成式「画里藏字」都不在覆盖范围——
后者在像素统计上跟正常图没区别，工具会判 clean，这是能力边界不是 bug。
"""
import json
import math
import os
import sys
from collections import Counter

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import _venv; _venv.ensure(guard="PIL")

from PIL import Image

Image.MAX_IMAGE_PIXELS = 200_000_000

# RS 分析用的两个翻转函数：F1 把 0<->1、2<->3…（等于 x^1）；F-1 把 -1<->0、1<->2…（等于 F1(x+1)-1）。
# 端点 0/255 不翻（标准做法，避免越界，影响可忽略）。
_RS_MASKS = ((0, 1, 1, 0), (0, -1, -1, 0))

PNG_TEXT_CHUNKS = {b"tEXt", b"zTXt", b"iTXt", b"eXIf", b"pCAL", b"sPLT"}

# RS 均值到多少算「值得看一眼」。0.10 = 干净负样本实测最大值（0.017）的六倍左右。
# 两个参考打得很凶（JPEG 参考自己就漂了）时要求高得多：0.20。
RS_HINT_MIN_P = 0.10
RS_HINT_STRONG_P = 0.20
# 噪点/纹理太重时 RS 根本不可信（实测加 σ≥4 高斯噪点后假阳一大片），这时不出第三档。
# 噪点估计用 Immerkær 算子（3×3 高通绝对值均值），实测干净真图 0.3–6.5，加 σ3 噪点 2.0–6.6。
RS_HINT_MAX_SIGMA = 3.0


def container_trace(path):
    """不靠 PIL，直接读字节看容器层面有没有夹带。"""
    with open(path, "rb") as f:
        data = f.read()
    out = {"bytes": len(data), "format": None, "trailing": 0, "chunks": [], "app_segments": []}
    if data[:8] == b"\x89PNG\r\n\x1a\n":
        out["format"] = "png"
        pos, chunks = 8, []
        while pos + 8 <= len(data):
            ln = int.from_bytes(data[pos:pos + 4], "big")
            typ = data[pos + 4:pos + 8]
            body = data[pos + 8:pos + 8 + ln]
            if typ in PNG_TEXT_CHUNKS:
                chunks.append({"type": typ.decode("ascii"), "len": ln,
                               "preview": body[:80].decode("utf-8", "replace")})
            if typ == b"IEND":
                out["trailing"] = len(data) - (pos + 12)
                break
            pos += 12 + ln
        out["chunks"] = chunks
    elif data[:2] == b"\xff\xd8":
        out["format"] = "jpeg"
        i = 2
        while i + 4 <= len(data):
            if data[i] != 0xFF:
                i += 1
                continue
            marker = data[i + 1]
            if marker in (0xD8, 0xD9) or 0xD0 <= marker <= 0xD7:
                if marker == 0xD9:
                    out["trailing"] = len(data) - (i + 2)
                    break
                i += 2
                continue
            ln = int.from_bytes(data[i + 2:i + 4], "big")
            if 0xE0 <= marker <= 0xEF or marker == 0xFE:
                body = data[i + 4:i + 2 + ln]
                name = body[:5].split(b"\x00")[0].decode("latin-1") if marker != 0xFE else "COM"
                out["app_segments"].append({"marker": hex(marker), "name": name, "len": ln})
            if marker == 0xDA:  # SOS 之后是熵编码数据，不再逐段解析
                eoi = data.rfind(b"\xff\xd9")
                out["trailing"] = len(data) - (eoi + 2) if eoi >= 0 else 0
                break
            i += 2 + ln
    else:
        out["format"] = "other"
    return out


def noise_sigma(path, max_pixels=400_000):
    """Immerkær 噪点估计：3×3 高通绝对值和归一化。只读灰度，与 LSB 无关。

    实测（800×600）：干净真照片 0.3–6.5，桌面截图 0.4，纯渐变 0.0；
    人为加高斯噪点 σ3 后 2.0–6.6、σ8 后 4.9–7.0。所以它是「能不能信 RS」的间门，
    不是「有没有噪点」的度量 —— 纹理密的图也会高。
    """
    im = Image.open(path).convert("L")
    im.load()
    w, h = im.size
    step = max(1, int((w * h / max_pixels) ** 0.5))
    px = im.load()
    total = 0
    n = 0
    for y in range(1, h - 1, step):
        for x in range(1, w - 1, step):
            v = (px[x - 1, y - 1] - 2 * px[x, y - 1] + px[x + 1, y - 1]
                 - 2 * px[x - 1, y] + 4 * px[x, y] - 2 * px[x + 1, y]
                 + px[x - 1, y + 1] - 2 * px[x, y + 1] + px[x + 1, y + 1])
            total += abs(v)
            n += 1
    if not n:
        return None
    return round((3.141592653589793 / 2) ** 0.5 * total / (6.0 * n), 3)


def chi2_sf(x, df):
    """卡方生存函数，Wilson-Hilferty 近似（df 大时误差 <1%）。无 scipy 依赖。"""
    if x <= 0:
        return 1.0
    h = 2.0 / (9.0 * df)
    z = ((x / df) ** (1.0 / 3.0) - (1.0 - h)) / math.sqrt(h)
    return 0.5 * math.erfc(z / math.sqrt(2.0))


def _plane_metrics(hh, ones, match, pairs):
    n = sum(hh.values())
    if not n:
        return None
    chi = 0.0
    for i in range(128):
        a, b = hh[2 * i], hh[2 * i + 1]
        if a + b:
            chi += (a - b) ** 2 / (a + b)
    po = ones / n
    ent = 0.0 if po in (0.0, 1.0) else -(po * math.log2(po) + (1 - po) * math.log2(1 - po))
    return {
        "chi2": round(chi, 1),
        "chi2_norm": round(chi / 127.0, 3),
        "chi2_p": round(chi2_sf(chi, 127), 4),
        "lsb_entropy": round(ent, 5),
        "lsb_one_ratio": round(po, 4),
        "neighbor_match": round(match / pairs, 4) if pairs else None,
    }


def lsb_stats(path, max_pixels=400_000, bands=16):
    """整图 + 按行分段的 LSB 统计。

    为什么要分段：顺序嵌入（大多数简易工具的做法）只污染文件前一段，整图统计会被干净区域稀释，
    实测同一张图嵌入 10% 容量时整图 chi2/df 反而升高。分段后那几段会单独掉到 chi2/df≈1。
    """
    im = Image.open(path)
    im.load()
    mode_note = im.mode
    im = im.convert("RGB")
    w, h = im.size
    px = im.load()
    total = w * h
    step = max(1, int(math.isqrt(total // max_pixels)) if total > max_pixels else 1)
    nb = max(4, bands)
    band_h = max(1, math.ceil(h / nb))
    acc = [[{"hist": Counter(), "ones": 0, "match": 0, "pairs": 0} for _ in range(3)] for _ in range(nb)]
    for y in range(0, h, step):
        bi = min(nb - 1, y // band_h)
        prev = None
        for x in range(0, w, step):
            r, g, b = px[x, y]
            for c, v in enumerate((r, g, b)):
                cell = acc[bi][c]
                cell["hist"][v] += 1
                cell["ones"] += v & 1
                if prev is not None:
                    cell["pairs"] += 1
                    if (v & 1) == (prev[c] & 1):
                        cell["match"] += 1
            prev = (r, g, b)
    names = "RGB"
    whole = {"hist": [Counter() for _ in range(3)], "ones": [0] * 3, "match": [0] * 3, "pairs": [0] * 3}
    band_rows = []
    for bi in range(nb):
        row = []
        for c in range(3):
            cell = acc[bi][c]
            row.append(_plane_metrics(cell["hist"], cell["ones"], cell["match"], cell["pairs"]))
            whole["hist"][c].update(cell["hist"])
            whole["ones"][c] += cell["ones"]
            whole["match"][c] += cell["match"]
            whole["pairs"][c] += cell["pairs"]
        band_rows.append(row)
    res = {"size": [w, h], "mode": mode_note, "bands": nb,
           "samples": sum(sum(cnt.values()) for cnt in whole["hist"]), "planes": {}}
    for c in range(3):
        res["planes"][names[c]] = _plane_metrics(whole["hist"][c], whole["ones"][c],
                                                 whole["match"][c], whole["pairs"][c])
    # 分段里最像被嵌入的那段：chi2/df 最小且邻同率低
    suspects = []
    for bi, row in enumerate(band_rows):
        for c, m in enumerate(row):
            if not m:
                continue
            if m["chi2_norm"] < 3.0 and (m["neighbor_match"] or 1) < 0.55 and m["lsb_entropy"] > 0.999:
                suspects.append({"band": bi, "y_range": [bi * band_h, min(h, (bi + 1) * band_h)],
                                 "channel": names[c], "chi2_norm": m["chi2_norm"],
                                 "neighbor_match": m["neighbor_match"]})
    res["band_suspects"] = suspects
    norms = [m["chi2_norm"] for row in band_rows for m in row if m]
    res["band_norm_median"] = round(sorted(norms)[len(norms) // 2], 3) if norms else None
    res["band_norm_min"] = min(norms) if norms else None
    med, mn = res["band_norm_median"], res["band_norm_min"]
    # 判据：既要有段落到「像随机 LSB」的区间，又要跟全图多数段拉开差距（避免本身就平坦的大色块图误报）
    res["embed_ratio"] = round(med / mn, 2) if med and mn else None
    res["embed_suspect"] = bool(suspects and med and mn and med / mn > 2.0)
    return res


def _rs_counts(rows, mask):
    """对每个 4 像素组：判别函数 f=|a-b|+|b-c|+|c-d|，掩码处翻转后 f 变大记 R、变小记 S。"""
    f1 = lambda x: x ^ 1
    fm1 = lambda x: (((x + 1) ^ 1) - 1) if 0 < x < 255 else x
    flips = [f1 if m == 1 else (fm1 if m == -1 else None) for m in mask]
    R = S = U = 0
    for row in rows:
        g = [row[o::4] for o in range(4)]
        n = min(len(x) for x in g)
        if n <= 0:
            continue
        a, b, c, d = (x[:n] for x in g)
        for i in range(n):
            p0, p1, p2, p3 = a[i], b[i], c[i], d[i]
            f = abs(p0 - p1) + abs(p1 - p2) + abs(p2 - p3)
            q = [p0, p1, p2, p3]
            for k, fn in enumerate(flips):
                if fn is not None:
                    q[k] = fn(q[k])
            fv = abs(q[0] - q[1]) + abs(q[1] - q[2]) + abs(q[2] - q[3])
            if fv > f:
                R += 1
            elif fv < f:
                S += 1
            else:
                U += 1
    return R, S, U


def _rs_scan(img, max_pixels=300_000):
    """返回每个通道的两个 d：正掩码 d+ = R-M 减 S-M，负掩码 d-（同样算法、掩码取反）。"""
    im = img.convert("RGB")
    w, h = im.size
    px = im.load()
    step = max(1, int((w * h / max_pixels) ** 0.5))
    out = {name: {"plus": 0, "minus": 0} for name in "RGB"}
    for ch, name in enumerate("RGB"):
        rows = [[px[x, y][ch] for x in range(w)] for y in range(0, h, step)]
        for key, mask in zip(("plus", "minus"), _RS_MASKS):
            R, S, _ = _rs_counts(rows, mask)
            out[name][key] = R - S
    return out


def rs_stats(path, max_pixels=300_000):
    """RS 分析（Fridrich 2001）估 LSB 替换比例。

    d = R - S 随嵌入率线性下降到 0，所以 p = 1 - d/d_ref。难点全在 d_ref：
    - 经典做法用「负掩码的 d」当参考。实测在结构强的图（截图/线稿）上会反向漂移：
      某张截图嵌入 0.3 后负掩码的 d 反而从 31505 涨到 64093，估出的 p 直接是负的。
    - 改用「JPEG q90 往返一遍再算 d」：重压抹掉 LSB 但保留结构。在 6 张真实图上
      d_ref 跨嵌入率几乎不动（±1%），且贴近干净图真值；但对极高细节的照片会高估 d_ref
      （一张 1456×816 噪点照真值 84399，重压后算出 146742），单用它会假阳。
    所以两个参考都算，取**较小**的 p：真实嵌入时两边一致（实测 0.1/0.2/0.3/0.5 两边差
    <0.01），参考价格崩渍时取小的那个正好把假阳压下去。代价是结构图会漏（宁可漏不误报），
    两边差 >0.15 时标 unreliable。
    """
    im = Image.open(path)
    im.load()
    im = im.convert("RGB")
    cur = _rs_scan(im, max_pixels)
    import io
    buf = io.BytesIO()
    try:
        im.save(buf, "JPEG", quality=90)
        buf.seek(0)
        ref = _rs_scan(Image.open(buf), max_pixels)
        ref_ok = True
    except Exception:  # noqa: BLE001
        ref, ref_ok = None, False
    planes = {}
    for name in "RGB":
        d = cur[name]["plus"]
        d_jpeg = ref[name]["plus"] if ref_ok else None
        d_minus = cur[name]["minus"]
        p_jpeg = None if not d_jpeg else max(0.0, min(1.0, 1.0 - d / d_jpeg))
        p_minus = None if not d_minus else max(0.0, min(1.0, 1.0 - d / d_minus))
        cands = [x for x in (p_jpeg, p_minus) if x is not None]
        planes[name] = {
            "d": d, "d_jpeg": d_jpeg, "d_minus": d_minus,
            "p_jpeg": None if p_jpeg is None else round(p_jpeg, 3),
            "p_minus": None if p_minus is None else round(p_minus, 3),
            "p": round(min(cands), 3) if cands else None,
        }
    ps = [v["p"] for v in planes.values() if v["p"] is not None]
    disagree = max((abs(v["p_jpeg"] - v["p_minus"]) for v in planes.values()
                    if v["p_jpeg"] is not None and v["p_minus"] is not None), default=0.0)
    return {"size": list(im.size), "ref": "jpeg90+minus" if ref_ok else "minus",
            "planes": planes, "max_p": max(ps) if ps else None,
            "mean_p": round(sum(ps) / len(ps), 3) if ps else None,
            "ref_disagree": round(disagree, 3), "reliable": disagree <= 0.15}


def scan(path):
    rep = {"path": path}
    try:
        rep["container"] = container_trace(path)
    except Exception as e:  # noqa: BLE001
        rep["container"] = {"error": repr(e)}
    try:
        rep["lsb"] = lsb_stats(path)
    except Exception as e:  # noqa: BLE001
        rep["lsb"] = {"error": repr(e)}
    try:
        rep["noise_sigma"] = noise_sigma(path)
    except Exception as e:  # noqa: BLE001
        rep["noise_sigma"] = None
        rep["noise_error"] = repr(e)
    try:
        rep["rs"] = rs_stats(path)
    except Exception as e:  # noqa: BLE001
        rep["rs"] = {"error": repr(e)}
    c, l = rep.get("container", {}), rep.get("lsb", {})
    flags = []
    if c.get("trailing"):
        flags.append(f"尾部夹带 {c['trailing']} 字节")
    if c.get("chunks"):
        flags.append("PNG 文本块 " + ",".join(ch["type"] for ch in c["chunks"]))
    for seg in c.get("app_segments", []):
        if seg["len"] > 20000 or seg["marker"] == "0xfe":
            flags.append(f"{seg['name']} 段 {seg['len']} 字节")
    # LSB 统计只作提示，不进判定：实测「细节丰富的照片」自然状态下 LSB 就接近随机，
    # 纯统计区分不了「内容本来就吵」和「被位替换过」（见笔记第 46 节的对照表）。
    rep["lsb_screen"] = None
    if isinstance(l, dict) and l.get("band_norm_min") is not None:
        rep["lsb_screen"] = (f"LSB 分段 chi2/df 最小 {l['band_norm_min']:.2f} / 中位 {l['band_norm_median']:.2f}"
                             f"（<2 且相邻位一致率≈0.50 时值得人工看，但不是证据）")
    r = rep.get("rs") or {}
    if r.get("mean_p") is not None:
        per = " ".join(f"{k}={v['p']:.2f}" for k, v in r["planes"].items() if v["p"] is not None)
        warn = "" if r.get("reliable") else "，两参考分歧大"
        noise = "" if rep.get("noise_sigma") is None else f"，噪声估计 {rep['noise_sigma']}"
        rep["rs_screen"] = (f"RS 嵌入率估计 {per}（均值 {r['mean_p']:.2f}{warn}{noise}；平滑图±0.02，"
                            f"结构化图偏低、0.1 基本测不出，只提示不判词）")
    # 第三档只看「统计上像做过大比例位替换」。
    # 两个参考分歧大时（ref_disagree >0.15）按说宁可不报，但单平面嵌入（只改 R 通道）
    # 正好会把 JPEG 参考拉偏 —— 20 张真图单平面嵌 50% 里有 3 张分歧 0.23–0.26 而均值 0.21–0.49。
    # 所以给个高门槛后门：均值 ≥0.20 就不看分歧了。负样本上两条路径都是 0/20 误报。
    #
    # 噪点闸门（2026-10-24 04:00 补）：重度噪点图会把 LSB 天然随机化，统计上跟嵌入一模一样。
    # 实测 σ≥4 高斯噪点在上面的规则下会大面积假阳（n237-s15 甚至 RS 均值 1.000、两参考完全一致），
    # 所以噪声估计 >RS_HINT_MAX_SIGMA 时不出第三档，宁可漏。
    hint = None
    sigma = rep.get("noise_sigma")
    if (not flags and r.get("mean_p") is not None and r["mean_p"] >= RS_HINT_MIN_P
            and sigma is not None and sigma <= RS_HINT_MAX_SIGMA):
        if r.get("reliable") or r["mean_p"] >= RS_HINT_STRONG_P:
            hint = "WORTH_A_LOOK"
    rep["hint"] = hint
    if (hint is None and not flags and r.get("mean_p") is not None
            and r["mean_p"] >= RS_HINT_MIN_P and sigma is not None
            and sigma > RS_HINT_MAX_SIGMA):
        rep["hint_suppressed"] = f"噪声估计 {sigma} > {RS_HINT_MAX_SIGMA}，RS 在此图上不可信"
    rep["verdict"] = "SUSPECT" if flags else (hint or "clean")
    rep["flags"] = flags
    return rep


def human(rep):
    c = rep.get("container", {})
    l = rep.get("lsb", {})
    head = f"{rep['path']}  {rep['verdict']}"
    if rep["flags"]:
        head += "  [" + "; ".join(rep["flags"]) + "]"
    elif rep.get("hint"):
        head += "  [大比例位替换的统计特征，建议人工看，不是证据]"
    if "size" in l:
        head += f"\n  {l['size'][0]}x{l['size'][1]} {l['mode']} 样本 {l['samples']}（三通道合计） 格式 {c.get('format')} {c.get('bytes')}B"
        for name, v in l["planes"].items():
            head += (f"\n  {name}: chi2/df={v['chi2_norm']:>7.3f} p={v['chi2_p']:<6.4f} "
                     f"熵={v['lsb_entropy']:.4f} 1占比={v['lsb_one_ratio']:.3f} 邻同={v['neighbor_match']}")
    if c.get("trailing"):
        head += f"\n  尾部多余字节: {c['trailing']}"
    if rep.get("lsb_screen"):
        head += "\n  " + rep["lsb_screen"]
    if rep.get("rs_screen"):
        head += "\n  " + rep["rs_screen"]
    if rep.get("hint_suppressed"):
        head += "\n  第三档未给: " + rep["hint_suppressed"]
    for ch in c.get("chunks", []):
        head += f"\n  文本块 {ch['type']} {ch['len']}B: {ch['preview'][:60]!r}"
    for seg in c.get("app_segments", []):
        head += f"\n  JPEG 段 {seg['marker']} {seg['name']} {seg['len']}B"
    for k in ("container", "lsb"):
        if "error" in rep.get(k, {}):
            head += f"\n  {k} 读失败: {rep[k]['error']}"
    return head


def embed_test(out_png, rate=1.0, seed_bits=None, source=None):
    """造对照组：同一张图，一份原样、一份顺序 LSB 替换藏 bits。rate=占像素容量的比例。"""
    import hashlib
    import random
    src = _base_image(source)
    ctl_png = out_png.replace(".png", "-control.png")
    src.save(ctl_png)
    payload = ("LSB replacement is measurable in pixel statistics; this line is the payload."
               * 3).encode() + b"\x00\x00"
    bits = [(b >> (7 - i)) & 1 for b in payload for i in range(8)]
    cap = src.size[0] * src.size[1]
    n = int(cap * rate)
    if n > len(bits):
        rng = random.Random(seed_bits or 1)
        bits = bits + [rng.getrandbits(1) for _ in range(n - len(bits))]
    else:
        bits = bits[:n]
    px = src.load()
    idx = 0
    for y in range(src.size[1]):
        for x in range(src.size[0]):
            if idx >= len(bits):
                break
            r, g, b = px[x, y]
            px[x, y] = ((r & ~1) | bits[idx], g, b)
            idx += 1
    src.save(out_png)
    return ctl_png, out_png, hashlib.md5(payload).hexdigest()[:8], len(bits)


def _base_image(source=None):
    """对照组底图：给了 --source 就用那张，否则现场造一张有渐变与低频纹理的图。

    为什么不内置一张照片：仓里带大图没必要；纯噪声图又会让 Immerkær 噪声估计飞高、
    扫描器直接判定「噪点图不可判」，当阴性对照反而不准。这里用「渐变 + 低频正弦 +
    ±1 抖动」，在高通噪声估计与 LSB 卡方上都与真照片同一档。
    """
    import math
    import random
    if source:
        return Image.open(source).convert("RGB")
    w, h = 800, 500
    rng = random.Random(20261024)
    im = Image.new("RGB", (w, h))
    px = im.load()
    for y in range(h):
        for x in range(w):
            g = 40 + (150 * x) // w + (30 * y) // h
            d = 25.0 * (math.sin(x / 9.0) + math.sin(y / 13.0) + 0.5 * math.sin((x + y) / 31.0))
            n = rng.randint(-1, 1)
            px[x, y] = (max(0, min(255, int(g + d + n))),
                        max(0, min(255, int(g - 10 + 0.8 * d + n))),
                        max(0, min(255, int(g + 12 + 1.2 * d + n))))
    return im


def main(argv):
    if not argv:
        print(__doc__)
        return 2
    if argv[0] == "--rate-test":
        print("嵌入率对 LSB 统计的影响（800x500 合成底图，顺序替换 R 通道）")
        print(f"{'rate':>6} {'R_chi/df':>9} {'R_p':>7} {'R_ent':>8} {'R_nbr':>7} {'G_norm':>8} {'B_norm':>8} verdict")
        for rate in (0.0, 0.001, 0.01, 0.05, 0.1, 0.25, 0.5, 0.75, 1.0):
            _, emb, _, _ = embed_test(f"/tmp/steg-r{int(rate*1000)}.png", rate=rate)
            r = scan(emb)
            pl = r["lsb"]["planes"]
            print(f"{rate:>6.3f} {pl['R']['chi2_norm']:>9.3f} {pl['R']['chi2_p']:>7.4f} {pl['R']['lsb_entropy']:>8.4f} "
                  f"{pl['R']['neighbor_match']:>7.4f} {pl['G']['chi2_norm']:>8.3f} {pl['B']['chi2_norm']:>8.3f} {r['verdict']}")
        return 0
    if argv[0] == "--rs-test":
        print("RS 分析标定：同一张图嵌入不同比例的随机 bit（R 通道顺序替换），看估出来多少")
        print(f"{'rate':>6} {'R':>6} {'G':>6} {'B':>6} {'mean':>6}  R: jpeg/-M  d/d_jpeg")
        for rate in (0.0, 0.05, 0.1, 0.2, 0.3, 0.5, 0.75, 1.0):
            p = f"/tmp/steg-rs{int(rate*1000)}.png"
            embed_test(p, rate=rate)
            r = rs_stats(p)
            pl = r["planes"]
            print(f"{rate:>6.2f} {pl['R']['p']:>6.2f} {pl['G']['p']:>6.2f} {pl['B']['p']:>6.2f} "
                  f"{r['mean_p']:>6.2f}  {pl['R']['p_jpeg']:>5.2f}/{pl['R']['p_minus']:<5.2f} "
                  f"{pl['R']['d']:>7}/{pl['R']['d_jpeg']:>7}")
        return 0
    if argv[0] == "--embed-test":
        ctl, emb, h, n = embed_test(argv[1] if len(argv) > 1 else "/tmp/steg-embedded.png")
        print(f"对照 {ctl} / 嵌入 {emb}（{n} bit, md5 {h}）")
        for p in (ctl, emb):
            print()
            print(human(scan(p)))
        return 0
    as_json = "--json" in argv
    as_table = "--table" in argv
    paths = [a for a in argv if a not in ("--json", "--table")]
    reps = [scan(p) for p in paths]
    if as_json:
        print(json.dumps(reps, ensure_ascii=False, indent=2))
    elif as_table:
        print(f"{'file':<26}{'size':>12}  {'R(p/nbr)':>16} {'G(p/nbr)':>16} {'B(p/nbr)':>16}  trailing  verdict")
        for r in reps:
            l = r["lsb"]
            if "error" in l:
                print(f"{r['path'][-25:]:<26}  ERROR {l['error']}")
                continue
            cells = " ".join(f"{v['chi2_p']:.3f}/{v['neighbor_match']:.3f}".rjust(16)
                             for v in l["planes"].values())
            size = f"{l['size'][0]}x{l['size'][1]}"
            print(f"{r['path'][-25:]:<26}{size:>12}  {cells}  {r['container'].get('trailing', 0):>8}  {r['verdict']}")
    else:
        for r in reps:
            print(human(r))
            print()
    return 1 if any(r["verdict"] == "SUSPECT" for r in reps) else 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
