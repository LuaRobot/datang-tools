#!/usr/bin/env python3
"""大数乘法实测台：CPython int(Karatsuba) vs libgmp(FFT)，量 log-log 斜率。

为什么：论文里的 O(n log n) 只是纸面复杂度，真实实现要看常数与交叉点。
GMP 用 ctypes 直接调 libgmp，不装 gmpy2。

用法：
  bigmul-scale.py                 # 默认 2^15..2^24 bits
  bigmul-scale.py --min 12 --max 22
  bigmul-scale.py --json
退出码：0 正常（含 GMP 缺失时只用 python 跑），1 GMP 自检失败。
"""
import argparse, ctypes, json, math, random, time

def load_gmp():
    try:
        g = ctypes.CDLL("libgmp.so.10")
    except OSError:
        return None

    class mpz(ctypes.Structure):
        _fields_ = [("_mp_alloc", ctypes.c_int), ("_mp_size", ctypes.c_int),
                    ("_mp_d", ctypes.POINTER(ctypes.c_ulong))]

    g.__gmpz_init2.argtypes = [ctypes.POINTER(mpz), ctypes.c_ulong]
    g.__gmpz_clear.argtypes = [ctypes.POINTER(mpz)]
    g.__gmpz_import.argtypes = [ctypes.POINTER(mpz), ctypes.c_size_t, ctypes.c_int,
                                ctypes.c_size_t, ctypes.c_int, ctypes.c_size_t, ctypes.c_void_p]
    g.__gmpz_mul.argtypes = [ctypes.POINTER(mpz), ctypes.POINTER(mpz), ctypes.POINTER(mpz)]
    g.__gmpz_export.argtypes = [ctypes.c_void_p, ctypes.POINTER(ctypes.c_size_t), ctypes.c_int,
                                ctypes.c_size_t, ctypes.c_int, ctypes.c_size_t, ctypes.POINTER(mpz)]
    return g, mpz

def gmp_new(g, mpz, nbits, data):
    x = mpz(); g.__gmpz_init2(ctypes.byref(x), nbits)
    buf = ctypes.create_string_buffer(data, len(data))
    g.__gmpz_import(ctypes.byref(x), len(data), -1, 1, 1, 0, buf)  # order=-1: 低字节在前
    return x

def gmp_bytes(g, mpz, x, cap):
    out = ctypes.create_string_buffer(cap); cnt = ctypes.c_size_t()
    g.__gmpz_export(out, ctypes.byref(cnt), -1, 1, 1, 0, ctypes.byref(x))
    return out.raw[:cnt.value]

def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--min", type=int, default=15, help="起始规模 2^N bits")
    ap.add_argument("--max", type=int, default=24, help="结束规模 2^N bits")
    ap.add_argument("--json", action="store_true")
    a = ap.parse_args()

    gmp = load_gmp()
    g = mpz = None
    if gmp:
        g, mpz = gmp
        # 自检：与 CPython 结果逐字节比对
        da = random.getrandbits(4096).to_bytes(512, "little")
        db = random.getrandbits(4096).to_bytes(512, "little")
        pa = gmp_new(g, mpz, 4096, da); pb = gmp_new(g, mpz, 4096, db)
        pr = mpz(); g.__gmpz_init2(ctypes.byref(pr), 8192)
        g.__gmpz_mul(ctypes.byref(pr), ctypes.byref(pa), ctypes.byref(pb))
        got = int.from_bytes(gmp_bytes(g, mpz, pr, 1100), "little")
        if got != int.from_bytes(da, "little") * int.from_bytes(db, "little"):
            print(json.dumps({"error": "gmp self-check failed"}) if a.json else "gmp 自检失败")
            return 1

    rows = []
    if not a.json:
        print(f"{'bits':>10} {'py ms':>10} {'gmp ms':>10} {'ratio':>7}")
    for k in range(a.min, a.max + 1):
        n = 1 << k
        da = random.getrandbits(n).to_bytes(n // 8, "little")
        db = random.getrandbits(n).to_bytes(n // 8, "little")
        x = int.from_bytes(da, "little"); y = int.from_bytes(db, "little")
        reps = 5 if n <= 1 << 21 else 3

        def py():
            t0 = time.perf_counter(); x * y; return time.perf_counter() - t0

        pt = min(py() for _ in range(reps))
        gt = None
        if g:
            pa = gmp_new(g, mpz, n, da); pb = gmp_new(g, mpz, n, db)
            pr = mpz(); g.__gmpz_init2(ctypes.byref(pr), 2 * n)

            def gm():
                t0 = time.perf_counter()
                g.__gmpz_mul(ctypes.byref(pr), ctypes.byref(pa), ctypes.byref(pb))
                return time.perf_counter() - t0

            gt = min(gm() for _ in range(reps))
            for z in (pa, pb, pr):
                g.__gmpz_clear(ctypes.byref(z))
        rows.append({"bits": n, "python_ms": round(pt * 1000, 4),
                     "gmp_ms": None if gt is None else round(gt * 1000, 4)})
        if not a.json:
            ratio = f"{pt/gt:7.1f}" if gt else "      -"
            gs = f"{gt*1000:10.3f}" if gt else "         -"
            print(f"{n:>10} {pt*1000:>10.3f} {gs} {ratio}", flush=True)

    def slope(key, r1, r2):
        if r1[key] is None or r2[key] is None:
            return None
        return round(math.log(r2[key] / r1[key]) / math.log(r2["bits"] / r1["bits"]), 3)

    slopes = []
    for r1, r2 in zip(rows, rows[1:]):
        slopes.append({"from": r1["bits"], "to": r2["bits"],
                       "python_p": slope("python_ms", r1, r2),
                       "gmp_p": slope("gmp_ms", r1, r2)})
    if a.json:
        print(json.dumps({"rows": rows, "slopes": slopes, "gmp": bool(g)}, ensure_ascii=False))
    else:
        print("\n局部指数（t ~ n^p）：")
        for s in slopes:
            gp = "-" if s["gmp_p"] is None else f"{s['gmp_p']:.3f}"
            print(f"  {s['from']:>9}->{s['to']:<9} python p={s['python_p']:.3f}  gmp p={gp}")
    return 0

if __name__ == "__main__":
    raise SystemExit(main())
