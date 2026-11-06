#!/usr/bin/env python3
"""耳机裁剪（ear clipping）实验台 —— 复现 globus.software 那篇 earcut 加速文章的中间结论。

2026-10-22 夜：文章（https://globus.software/blog/2026/10/16/cutting-map-polygons/）
逐步优化三角化：naive ears 1119ms → 只测凹点 882ms → 凸多边形捷径 880 → 三角带 762
→ 分桶 697 → 无分支计数 519 → 标记命中 489（3.74M 多边形，M4 Pro 单核）。

本脚本不追 C++ 绝对速度，只验两条可证伪的说法：
  1. 正确性：三角数 = n-2、三角形面积和 = 多边形面积、无退化三角形。
  2. 「只测凹点」在小多边形上反而更慢（排序/维护凹点表比直接扫还贵），
     大多边形上明显更快 —— 用同样的结构分布量一遍，看斜率对不对。
附带统计耳测试次数与被扫描点数，跟文章的量级对比。

用法： python3 earclip-lab.py [--json] [--seed N] [--scale N]
"""
import argparse
import json
import math
import random
import sys
import time


# ---------- 多边形生成 ----------

def _star_shaped(n, rmin, rmax, rng):
    """径向随机 + 角度递增 → 保证简单（星形域）。

    坐标保浮点：早期版本 round 成整数，n 大时极角误差会把相邻点的角度顺序拗反
    （实测 n=120 出现 1 处自交），所以这里不取整。
    """
    angs = sorted(rng.uniform(0, 2 * math.pi) for _ in range(n))
    pts = []
    for a in angs:
        r = rng.uniform(rmin, rmax)
        pts.append((round(r * math.cos(a), 3), round(r * math.sin(a), 3)))
    return _dedupe(pts)


def _spiky(n, rng, teeth=8):
    """内圈/外圈交替 → 大量凹点，模拟建筑/森林轮廓。坐标保浮点（同 _star_shaped 的取整坑）。"""
    pts = []
    for i in range(n):
        a = 2 * math.pi * i / n
        r = 100 if i % 2 == 0 else rng.uniform(55, 75)
        pts.append((round(r * math.cos(a), 3), round(r * math.sin(a), 3)))
    return _dedupe(pts)


def _comb(teeth, rng):
    """锯齿山脉：x 严格递增 → 天然简单，每齿一个凹谷（最坏凹点密度）。"""
    pts = [(0.0, 0.0)]
    for k in range(2 * teeth):
        pts.append(((k + 1) * 10.0, 100.0 if k % 2 == 0 else rng.uniform(25, 45)))
    pts.append((2 * teeth * 10.0, 0.0))
    return _dedupe(pts)


def _dedupe(pts):
    out = []
    for p in pts:
        if not out or out[-1] != p:
            out.append(p)
    if len(out) > 1 and out[0] == out[-1]:
        out.pop()
    return out


def _jitter(pts, amp=0.01):
    """坐标微扰，避免三点共线/重合 —— 共线会让耳测试退化成零面积三角形。

    真实地图数据里共线其实很常见（文章专门讲整数网格线上的射线），本实验先
    排除退化，专测「耳测试要扫多少点」这一件事。
    """
    rnd = random.Random(20261022)
    # 同一坐标必须给同一偏移，否则重合点被拗开 → 自交
    off = {}
    out = []
    for x, y in pts:
        o = off.get((x, y))
        if o is None:
            o = (rnd.uniform(-amp, amp), rnd.uniform(-amp, amp))
            off[(x, y)] = o
        out.append((x + o[0], y + o[1]))
    return out


def area2(poly):
    s = 0
    n = len(poly)
    for i in range(n):
        x1, y1 = poly[i]
        x2, y2 = poly[(i + 1) % n]
        s += x1 * y2 - x2 * y1
    return s  # 2×有符号面积


def normalize(poly):
    """统一成逆时针（面积 > 0）。"""
    p = _dedupe(poly)
    return p if area2(p) > 0 else p[::-1]


# ---------- 裁剪器 ----------

class Stats:
    __slots__ = ("ear_tests", "points_scanned", "tris", "created_reflex")

    def __init__(self):
        self.ear_tests = 0
        self.points_scanned = 0
        self.tris = 0
        self.created_reflex = 0


def _cross(ax, ay, bx, by, cx, cy):
    return (bx - ax) * (cy - ay) - (by - ay) * (cx - ax)


def is_simple(poly):
    """多边形不自交（O(n²)，只用于验证）。"""
    n = len(poly)

    def seg_int(p1, p2, p3, p4):
        d1 = _cross(p3[0], p3[1], p4[0], p4[1], p1[0], p1[1])
        d2 = _cross(p3[0], p3[1], p4[0], p4[1], p2[0], p2[1])
        d3 = _cross(p1[0], p1[1], p2[0], p2[1], p3[0], p3[1])
        d4 = _cross(p1[0], p1[1], p2[0], p2[1], p4[0], p4[1])
        return ((d1 > 0) != (d2 > 0)) and ((d3 > 0) != (d4 > 0))

    for i in range(n):
        for j in range(i + 1, n):
            if j == i or (j + 1) % n == i or (i + 1) % n == j:
                continue
            if seg_int(poly[i], poly[(i + 1) % n], poly[j], poly[(j + 1) % n]):
                return False
    return True


def clip(poly, dents_only=False):
    """耳裁剪。dents_only=True 时耳测试只看凹点（文章的第二步优化）。"""
    n = len(poly)
    st = Stats()
    if n < 3:
        return [], st
    xs = [p[0] for p in poly]
    ys = [p[1] for p in poly]
    nxt = [(i + 1) % n for i in range(n)]
    prv = [(i - 1) % n for i in range(n)]
    alive = [True] * n
    live = n
    tris = []

    reflex = set()
    for i in range(n):
        a, b, c = prv[i], i, nxt[i]
        if _cross(xs[a], ys[a], xs[b], ys[b], xs[c], ys[c]) <= 0:
            reflex.add(i)

    created_reflex = 0

    def is_ear(v):
        st.ear_tests += 1
        a, b, c = prv[v], v, nxt[v]
        ax, ay, bx, by, cx, cy = xs[a], ys[a], xs[b], ys[b], xs[c], ys[c]
        if _cross(ax, ay, bx, by, cx, cy) <= 0:
            return False
        if dents_only:
            cand = reflex
        else:
            cand = range(n)
        w = nxt[c] if not dents_only else None
        if dents_only:
            for p in cand:
                if p == a or p == b or p == c:
                    continue
                px, py = xs[p], ys[p]
                st.points_scanned += 1
                if _cross(ax, ay, bx, by, px, py) < 0:
                    continue
                if _cross(bx, by, cx, cy, px, py) < 0:
                    continue
                if _cross(cx, cy, ax, ay, px, py) < 0:
                    continue
                return False
            return True
        # 朴素版：沿边界走一圈
        while w != b:
            px, py = xs[w], ys[w]
            st.points_scanned += 1
            if (_cross(ax, ay, bx, by, px, py) >= 0
                    and _cross(bx, by, cx, cy, px, py) >= 0
                    and _cross(cx, cy, ax, ay, px, py) >= 0):
                if not ((px == ax and py == ay) or (px == bx and py == by)
                        or (px == cx and py == cy)):
                    return False
            w = nxt[w]
        return True

    def unlink(v):
        nonlocal live, created_reflex
        a, c = prv[v], nxt[v]
        nxt[a] = c
        prv[c] = a
        alive[v] = False
        live -= 1
        for z in (a, c):
            nz = _cross(xs[prv[z]], ys[prv[z]], xs[z], ys[z], xs[nxt[z]], ys[nxt[z]])
            was_reflex = z in reflex
            if nz <= 0:
                if not was_reflex:
                    created_reflex += 1
                reflex.add(z)
            elif was_reflex:
                reflex.discard(z)

    v = 0
    fails = 0
    while live > 3:
        if is_ear(v):
            a, c = prv[v], nxt[v]
            tris.append((a, v, c))
            st.tris += 1
            unlink(v)
            v = a
            fails = 0
        else:
            v = nxt[v]
            fails += 1
            if fails > live + 2:  # 理论上不该发生：简单多边形必有耳
                return None, st
    if live == 3:
        a, b, c = v, nxt[v], nxt[nxt[v]]
        tris.append((a, b, c))
        st.tris += 1
    st.created_reflex = created_reflex
    return tris, st


def tri_area_sum(poly, tris):
    s = 0.0
    for a, b, c in tris:
        ax, ay = poly[a]
        bx, by = poly[b]
        cx, cy = poly[c]
        s += _cross(ax, ay, bx, by, cx, cy) / 2.0
    return s


def verify(poly):
    """返回 (ok, 说明)。两种裁剪器都要过；先确认输入是简单多边形。"""
    n = len(poly)
    if not is_simple(poly):
        return False, f"输入自交（n={n}）"
    for dents_only in (False, True):
        tris, st = clip(poly, dents_only)
        tag = "dents" if dents_only else "naive"
        if tris is None:
            return False, f"{tag}: 找不到耳（n={n}）"
        if len(tris) != n - 2:
            return False, f"{tag}: 三角数 {len(tris)} != n-2={n-2}"
        want = area2(poly) / 2.0
        got = tri_area_sum(poly, tris)
        if abs(got - want) > 1e-6 * max(1.0, abs(want)):
            return False, f"{tag}: 面积 {got:.6f} != {want:.6f}"
        for a, b, c in tris:
            ar = _cross(*poly[a], *poly[b], *poly[c]) / 2.0
            if ar <= 0:
                return False, f"{tag}: 出现非正面积三角形 {ar:.6g}"
    return True, "ok"


# ---------- 基准 ----------

def bench(groups, dents_only):
    t0 = time.perf_counter()
    tot = Stats()
    for polys in groups.values():
        for p in polys:
            tris, st = clip(p, dents_only)
            if tris is None:
                raise RuntimeError(f"裁剪失败 n={len(p)}")
            tot.ear_tests += st.ear_tests
            tot.points_scanned += st.points_scanned
            tot.tris += st.tris
            tot.created_reflex += st.created_reflex
    return (time.perf_counter() - t0) * 1000.0, tot


def make_groups(scale, seed):
    rng = random.Random(seed)
    g = {}
    g["<=4"] = []
    for i in range(600 * scale):
        if i % 2:
            g["<=4"].append(_jitter([(0, 0), (10, 0), (10, 10)]))
        else:
            g["<=4"].append(_jitter([(0, 0), (12, 0), (10, 9), (-2, 8)]))
    g["5-8"] = [_star_shaped(rng.randint(5, 8), 20, 60, rng) for _ in range(400 * scale)]
    g["9-32"] = [_spiky(rng.randint(9, 32), rng) for _ in range(200 * scale)]
    g["33-256"] = [_comb(rng.randint(12, 60), rng) for _ in range(40 * scale)]
    g["257+"] = [_comb(rng.randint(120, 160), rng) for _ in range(6 * scale)]
    out = {}
    dropped = 0
    for k, v in g.items():
        keep = []
        for p in v:
            q = normalize(_jitter(p))
            if len(q) >= 3 and is_simple(q):
                keep.append(q)
            else:
                dropped += 1
        out[k] = keep
    return out, dropped


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--json", action="store_true")
    ap.add_argument("--seed", type=int, default=7)
    ap.add_argument("--scale", type=int, default=1)
    ap.add_argument("--verify-only", action="store_true")
    args = ap.parse_args()

    rng = random.Random(args.seed)
    checks = []
    # 正确性：随机多边形 + 病态形状逐个核
    cases = [_star_shaped(n, 20, 80, rng) for n in (3, 4, 5, 7, 12, 40, 120)]
    cases += [_spiky(n, rng) for n in (9, 16, 64, 200)]
    cases += [_comb(k, rng) for k in (2, 5, 20, 90)]
    cases += [[(0, 0), (10, 0), (10, 10), (0, 10)]]           # 正方形
    cases += [[(0, 0), (100, 0), (100, 1), (0, 1)]]           # 极薄
    for p in cases:
        p = normalize(_jitter(p))
        ok, why = verify(p)
        checks.append({"n": len(p), "ok": ok, "why": why})
    bad = [c for c in checks if not c["ok"]]

    groups, dropped = make_groups(args.scale, args.seed)
    for k, polys in groups.items():
        bad_here = [p for p in polys[:20] if not verify(p)[0]]
        if bad_here:
            print(f"[!] {k} 组 {len(bad_here)}/20 个样本验证失败", file=sys.stderr)

    t_naive, s_naive = bench(groups, False)
    t_dents, s_dents = bench(groups, True)

    per = {}
    for k in groups:
        g1 = {k: groups[k]}
        a, sa = bench(g1, False)
        b, sb = bench(g1, True)
        per[k] = {"n_polys": len(groups[k]), "naive_ms": round(a, 1),
                  "dents_ms": round(b, 1), "naive_tests": sa.ear_tests,
                  "dents_tests": sb.ear_tests,
                  "naive_pts": sa.points_scanned, "dents_pts": sb.points_scanned}

    out = {
        "checks": {"total": len(checks), "failed": len(bad), "detail": bad[:5]},
        "dropped_synthetic": dropped,
        "total": {
            "polys": sum(len(v) for v in groups.values()),
            "naive_ms": round(t_naive, 1), "dents_ms": round(t_dents, 1),
            "naive_tests": s_naive.ear_tests, "dents_tests": s_dents.ear_tests,
            "naive_pts": s_naive.points_scanned, "dents_pts": s_dents.points_scanned,
            "tris": s_naive.tris,
            "created_reflex": s_naive.created_reflex,
        },
        "per_group": per,
    }
    if args.json:
        print(json.dumps(out, ensure_ascii=False, indent=2))
    else:
        print(f"正确性：{len(checks)} 个多边形，失败 {len(bad)}")
        for b in bad:
            print("  FAIL", b)
        t = out["total"]
        print(f"\n合成样本丢弃（自交/退化）：{dropped} 个")
        print(f"合计 {t['polys']} 个多边形 / {t['tris']} 个三角形")
        print(f"  朴素扫全边界: {t['naive_ms']} ms  耳测试 {t['naive_tests']:,}  扫点 {t['naive_pts']:,}")
        print(f"  只测凹点    : {t['dents_ms']} ms  耳测试 {t['dents_tests']:,}  扫点 {t['dents_pts']:,}")
        print(f"  提速 {t['naive_ms']/max(t['dents_ms'],1e-9):.2f}×  扫点降到 "
              f"{100.0*t['dents_pts']/max(t['naive_pts'],1):.1f}%")
        print(f"  新产生的凹点（耳裁剪不应造出凹点）：{t['created_reflex']}")
        print("\n分组（ms）：")
        for k, v in per.items():
            print(f"  {k:>7}: n={v['n_polys']:4d}  朴素 {v['naive_ms']:7.1f}  凹点 {v['dents_ms']:7.1f}"
                  f"  扫点 {v['naive_pts']:>9,} → {v['dents_pts']:>9,}")
    return 1 if bad else 0


if __name__ == "__main__":
    sys.exit(main())
