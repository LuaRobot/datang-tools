#!/usr/bin/env python3
"""fetch-page.py — 抓网页，自动跳过「元刷新式」拦截页。

起因：2026-10-25 夜里被 justice.gov 的 Akamai interstitial 挡住（返回 2.4 KB
空壳，标题是 &nbsp;，正文里只有一段 JS）。读那段 JS 发现它所谓的工作量证明只是
`var j = i + Number("1024" + "68077")`，而真正的通关凭据其实是挑战页 meta refresh
里那个带 `?bm-verify=` 的 URL —— 直接 GET 一次就拿到真页面，不用 POST、不用算、
换 UA、不带给 cookie 都认（实测同一 token 连取三次全 200，116 KB）。

所以这个工具只做两件事，都是「照浏览器做的下一跳再走一次」：
  1. Akamai interstitial：正文里出现 `bm-verify` 且是 meta refresh → 取 meta 里
     的 URL 再 GET 一次。
  2. 普通 meta refresh 跳转（`content="0;url=..."`，常见于老站点 301 替身）→ 同样跟一跳。
最多跟 3 跳，避免环路。

用法：
  ./fetch-page.py URL                     # 打印状态/大小/标题，正文另存
  ./fetch-page.py URL --text              # 直接打印去标签后的正文
  ./fetch-page.py URL -o out.html         # 指定保存路径
  ./fetch-page.py URL --json              # 机读（status/bytes/title/hops/file）
  ./fetch-page.py URL --proxy http://127.0.0.1:7890     # 默认读 http_proxy 环境变量

注意：这只是把「浏览器会做的下一跳」补上，不是绕过登录或频率限制。抓站请保持
低频，别拿它跑批量爬虫。
"""
from __future__ import annotations

import argparse
import html as htmllib
import json
import os
import re
import subprocess
import sys
import tempfile

DEFAULT_UA = ("Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) "
              "Chrome/156.0.0.0 Safari/537.36")

META_REFRESH = re.compile(
    r"""<meta[^>]+http-equiv=["']?refresh["']?[^>]*content=["']\s*\d+\s*;\s*url=['"]?([^"'>\s]+)""",
    re.I)

BLOCK_MARKERS = ("bm-verify", "akam-logo", "interstitial")


def run_curl(url: str, proxy: str | None, ua: str, extra: list[str] | None = None) -> tuple[int, bytes, dict]:
    # 头写到临时文件、体走 stdout：否则 -D - 与 -o - 混在一条流里，
    # 遇到 301 后接 200 时第一个空行后面还挂着第二段头，会把头当成正文。
    with tempfile.NamedTemporaryFile(prefix="fetch-page-hdr-") as hf:
        cmd = ["curl", "-sS", "-m", "45", "-A", ua, "-D", hf.name, "--compressed"]
        if proxy:
            cmd += ["-x", proxy]
        cmd += (extra or []) + [url]
        p = subprocess.run(cmd, capture_output=True)
        body = p.stdout
        hf.seek(0)
        head = hf.read()
    if not body and p.returncode != 0:
        return 0, p.stderr, {}
    # 多段响应（重定向链）取最后一段的头
    blocks = [b for b in head.split(b"\r\n\r\n") if b.strip()]
    head = blocks[-1] if blocks else b""
    status = 0
    hdrs: dict[str, str] = {}
    for line in head.decode("latin-1").splitlines():
        if line.startswith("HTTP/"):
            m = re.search(r"\s(\d{3})\s", line)
            if m:
                status = int(m.group(1))
        elif ":" in line:
            k, v = line.split(":", 1)
            hdrs[k.strip().lower()] = v.strip()
    return status, body, hdrs


def looks_blocked(body: bytes) -> bool:
    if len(body) > 8000:
        return False
    s = body.decode("utf-8", "replace")
    return any(m in s for m in BLOCK_MARKERS)


def strip_tags(s: str) -> str:
    s = re.sub(r"(?s)<(script|style|noscript)\b.*?</\1>", " ", s)
    s = re.sub(r"(?s)<!--.*?-->", " ", s)
    s = re.sub(r"(?s)<[^>]+>", " ", s)
    s = htmllib.unescape(s)
    s = re.sub(r"[ \t\r\f\v]+", " ", s)
    s = re.sub(r"\n\s*\n+", "\n", s)
    return s.strip()


def fetch(url: str, proxy: str | None, ua: str, max_hops: int = 3):
    from urllib.parse import urljoin
    hops, status, body, hdrs = [], 0, b"", {}
    for _ in range(max_hops + 1):
        status, body, hdrs = run_curl(url, proxy, ua)
        hops.append({"url": url, "status": status, "bytes": len(body)})
        if status in (301, 302, 303, 307, 308) and hdrs.get("location"):
            url = urljoin(url, hdrs["location"])
            continue
        text = body.decode("utf-8", "replace")
        m = META_REFRESH.search(text)
        if not m:
            break
        blocked = looks_blocked(body)
        if not blocked and len(body) > 8000:
            break                      # 正常页面里夹带的 meta refresh，不跟
        url = urljoin(url, htmllib.unescape(m.group(1)))
    return status, body, hops


def main(argv=None):
    ap = argparse.ArgumentParser(description="抓网页并自动跟过元刷新式拦截页")
    ap.add_argument("url")
    ap.add_argument("-o", "--output", help="正文保存路径（默认 /tmp/fetch-page-<pid>.html）")
    ap.add_argument("--text", action="store_true", help="打印去标签正文而不是状态摘要")
    ap.add_argument("--json", action="store_true", help="机读输出")
    ap.add_argument("--proxy", default=os.environ.get("http_proxy") or os.environ.get("https_proxy"))
    ap.add_argument("--ua", default=DEFAULT_UA)
    ap.add_argument("--max-hops", type=int, default=3)
    args = ap.parse_args(argv)

    status, body, hops = fetch(args.url, args.proxy, args.ua, args.max_hops)
    text = body.decode("utf-8", "replace")
    m = re.search(r"<title[^>]*>(.*?)</title>", text, re.S | re.I)
    title = htmllib.unescape(re.sub(r"\s+", " ", m.group(1))).strip() if m else ""

    out = args.output or tempfile.mktemp(prefix="fetch-page-", suffix=".html")
    with open(out, "wb") as f:
        f.write(body)

    result = {"url": args.url, "final_status": status, "bytes": len(body),
              "title": title, "hops": hops, "file": out,
              "still_blocked": looks_blocked(body)}

    if args.json:
        print(json.dumps(result, ensure_ascii=False, indent=1))
    elif args.text:
        print(strip_tags(text))
    else:
        print(f"{status} {len(body)}B  {title[:90]}")
        for i, h in enumerate(hops):
            arrow = "→" if i else " "
            print(f"  {arrow} hop{i}: {h['status']} {h['bytes']}B {h['url'][:110]}")
        print(f"  正文: {out}")
        if result["still_blocked"]:
            print("  ! 仍然像拦截页（短且含 bm-verify/akam 标记）")
    return 0 if status == 200 and not result["still_blocked"] else 1


if __name__ == "__main__":
    sys.exit(main())
