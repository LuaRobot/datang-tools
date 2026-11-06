#!/usr/bin/env python3
"""Minimal Gopher (RFC 1436) client + menu parser + reachability prober.

Usage:
  gopher-get.py gopher://gopher.floodgap.com/1/            # fetch menu, pretty print
  gopher-get.py gopher://sdf.org/0/users/foo/hello.txt    # fetch text
  gopher-get.py --raw <url>                               # raw bytes to stdout
  gopher-get.py --json <url>                              # parsed menu as JSON
  gopher-get.py --probe host[:port] [host2 ...]           # tcp reachability + banner
  gopher-get.py --search veronica2 "tui"                  # query type-7 gopher search

Options: --proxy HOST:PORT (HTTP CONNECT tunnel, works with clash mixed-port),
         --timeout SECS (default 12), --max-bytes (default 4 MiB),
         --tls (wrap the connection in TLS, i.e. gophers://)
"""
import argparse
import json
import socket
import ssl
import sys
from urllib.parse import urlsplit, unquote

DEFAULT_PORT = 70
MAX_BYTES_DEFAULT = 4 * 1024 * 1024


def parse_url(url):
    """gopher://host[:port]/[type][selector] -> (host, port, gtype, selector, tls)"""
    if "://" not in url:
        url = "gopher://" + url
    u = urlsplit(url)
    if u.scheme not in ("gopher", "gophers"):
        raise SystemExit(f"not a gopher URL: {url}")
    host = u.hostname
    port = u.port or DEFAULT_PORT
    path = u.path or ""
    if path.startswith("/"):
        path = path[1:]
    gtype = "1"
    if path and len(path) >= 1 and path[0] in "0123456789+ITg;hisd":
        gtype = path[0]
        path = path[1:]
    if path.startswith("/"):
        path = path[1:]
    # %09 in URL == tab in selector (RFC 4266)
    selector = unquote(path).replace("%09", "\t")
    if u.query:
        # RFC 4266: the URL query is sent as the TAB-separated search argument
        selector = (selector + "\t" + unquote(u.query)) if selector else unquote(u.query)
    return host, port, gtype, selector, u.scheme == "gophers"


def _connect(host, port, timeout, proxy, tls, insecure=False):
    if proxy:
        phost, _, pport = proxy.rpartition(":")
        s = socket.create_connection((phost.strip("[]"), int(pport)), timeout=timeout)
        req = f"CONNECT {host}:{port} HTTP/1.1\r\nHost: {host}:{port}\r\n\r\n"
        s.sendall(req.encode())
        buf = b""
        while b"\r\n\r\n" not in buf:
            chunk = s.recv(4096)
            if not chunk:
                raise ConnectionError("proxy closed during CONNECT")
            buf += chunk
            if len(buf) > 65536:
                raise ConnectionError("proxy CONNECT response too large")
        status = buf.split(b"\r\n", 1)[0]
        if b" 200 " not in status and not status.endswith(b" 200"):
            raise ConnectionError(f"proxy CONNECT failed: {status!r}")
    else:
        s = socket.create_connection((host, port), timeout=timeout)
    if tls:
        ctx = ssl.create_default_context()
        if insecure:
            ctx.check_hostname = False
            ctx.verify_mode = ssl.CERT_NONE
        s = ctx.wrap_socket(s, server_hostname=host)
    return s


def fetch(host, port, selector, timeout=12, proxy=None, tls=False, max_bytes=MAX_BYTES_DEFAULT,
          insecure=False):
    s = _connect(host, port, timeout, proxy, tls, insecure)
    try:
        s.sendall(selector.encode("utf-8") + b"\r\n")
        out = bytearray()
        while True:
            chunk = s.recv(65536)
            if not chunk:
                break
            out += chunk
            if len(out) > max_bytes:
                break
    finally:
        s.close()
    return bytes(out)


def parse_menu(data):
    """Split a type-1 menu into items. Returns list of dicts."""
    text = data.decode("utf-8", "replace")
    items = []
    for line in text.split("\r\n") if "\r\n" in text else text.split("\n"):
        if not line:
            continue
        if line == ".":
            break
        # Gopher+ info line: "+INFO: ..." / type '+'
        if line.startswith("+-"):
            continue
        if len(line) < 2:
            continue
        gtype = line[0]
        rest = line[1:]
        parts = rest.split("\t")
        display = parts[0]
        selector = parts[1] if len(parts) > 1 else ""
        host = parts[2] if len(parts) > 2 else ""
        port = parts[3] if len(parts) > 3 and parts[3].isdigit() else DEFAULT_PORT
        if gtype == "i":
            items.append({"type": "i", "display": display})
        elif gtype == "3":
            # error item; text follows as subsequent i lines usually
            items.append({"type": "3", "display": display, "error": selector})
        else:
            items.append({
                "type": gtype,
                "display": display,
                "selector": selector,
                "host": host or None,
                "port": port if host else None,
            })
    return items


TYPE_NAMES = {
    "0": "text", "1": "menu", "2": "ccso", "3": "error", "4": "binhex",
    "5": "dos", "6": "uuencode", "7": "search", "8": "telnet", "9": "binary",
    "+": "redundant", "T": "tn3270", "g": "gif", "I": "image", "h": "html",
    "i": "info", "s": "sound", ";": "video", "d": "doc", "c": "calendar",
}


def text_of(data):
    return data.decode("utf-8", "replace")


def probe(hostspecs, timeout, proxy, tls, insecure=False):
    results = []
    for spec in hostspecs:
        host, _, p = spec.partition(":")
        port = int(p) if p.isdigit() else DEFAULT_PORT
        rec = {"host": host, "port": port}
        try:
            data = fetch(host, port, "", timeout=timeout, proxy=proxy, tls=tls, max_bytes=8192,
                         insecure=insecure)
            rec["ok"] = True
            rec["bytes"] = len(data)
            head = data.decode("utf-8", "replace").split("\n")[:3]
            rec["head"] = [h.rstrip("\r") for h in head]
            rec["looks_like_menu"] = bool(data) and data[:1] in b"0123456789+i3Ihgd"
        except Exception as e:  # noqa: BLE001 - probe mode reports any failure
            rec["ok"] = False
            rec["error"] = f"{type(e).__name__}: {e}"
        results.append(rec)
    return results


def main():
    ap = argparse.ArgumentParser(description="Minimal Gopher client / menu parser / prober")
    ap.add_argument("target", nargs="?", help="gopher:// URL, or host when --probe")
    ap.add_argument("extra", nargs="*", help="extra hosts for --probe")
    ap.add_argument("--proxy", help="HOST:PORT HTTP CONNECT proxy")
    ap.add_argument("--timeout", type=float, default=12.0)
    ap.add_argument("--max-bytes", type=int, default=MAX_BYTES_DEFAULT)
    ap.add_argument("--raw", action="store_true", help="write raw response bytes to stdout")
    ap.add_argument("--json", action="store_true", help="machine-readable output")
    ap.add_argument("--tls", action="store_true", help="wrap connection in TLS (gophers://)")
    ap.add_argument("--insecure", action="store_true", help="skip TLS certificate verification")
    ap.add_argument("--probe", action="store_true", help="treat targets as host[:port] and test reachability")
    args = ap.parse_args()

    if not args.target:
        ap.error("need a gopher URL or a host")

    try:
        if args.probe:
            res = probe([args.target] + args.extra, args.timeout, args.proxy, args.tls, args.insecure)
            print(json.dumps(res, ensure_ascii=False, indent=2))
            return 0 if all(r["ok"] for r in res) else 1

        host, port, gtype, selector, url_tls = parse_url(args.target)
        data = fetch(host, port, selector, args.timeout, args.proxy, args.tls or url_tls,
                     args.max_bytes, args.insecure)

        if args.raw:
            sys.stdout.buffer.write(data)
            return 0

        if gtype in ("1", "7") or (gtype == "0" and b"\t" in data.split(b"\n")[0]):
            items = parse_menu(data)
            if args.json:
                print(json.dumps({"host": host, "port": port, "selector": selector,
                                  "type": gtype, "items": items}, ensure_ascii=False, indent=2))
            else:
                for it in items:
                    if it["type"] == "i":
                        print("  " + it["display"])
                    elif it["type"] == "3":
                        print(f"!! ERROR: {it['display']}")
                    else:
                        tag = TYPE_NAMES.get(it["type"], it["type"])
                        sel = it["selector"]
                        sel = sel if sel.startswith("/") else "/" + sel
                        print(f"[{it['type']} {tag:<9}] {it['display']}"
                              f"  -> {it['host']}:{it['port']} {sel}")
            return 0

        # non-menu payload
        text = text_of(data)
        if args.json:
            print(json.dumps({"host": host, "port": port, "selector": selector,
                              "type": gtype, "bytes": len(data), "text": text},
                             ensure_ascii=False, indent=2))
        else:
            sys.stdout.write(text)
            if text and not text.endswith("\n"):
                sys.stdout.write("\n")
        return 0
    except Exception as e:  # noqa: BLE001 - CLI: surface the error, non-zero exit
        print(f"error: {type(e).__name__}: {e}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
