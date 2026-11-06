#!/usr/bin/env python3
"""refcheck.py —— 机器核对论文参考文献。

给一篇论文的 DOI，把它 deposited 在 Crossref 的 reference list 逐条拿去核：
带 DOI 的查 DOI 是否真存在、标题作者是否对得上；不带 DOI 的先用
query.bibliographic 反查真实记录，查不到再按「期刊 + 年 + 卷(期)」拉期刊目录
（TOC）逐条比标题与首页，判断是「不存在」还是「作者张冠李戴」。

分档：
  OK          能解析到唯一真实记录，且标题/作者对得上
  MISMATCH    记录存在但元数据对不上（真作者配假标题、卷期页码安错…）
  NOT_FOUND   期刊/卷期可查，但目录里没有这篇（= 大概率不存在）
  BAD_DOI     DOI 本身打不开（doi.org 404）
  UNVERIFIED  期刊不存在/无卷期信息/取数被封，机器判不了

用法：
  ./refcheck.py --doi 10.1080/13614533.2026.2742162
  ./refcheck.py --refs refs.json          # 数组，元素是 Crossref reference 结构
  ./refcheck.py --doi ... --json -o out.json
  ./refcheck.py --refs refs.json --strict # UNVERIFIED 也算失败

退出码：0 全过；1 有 MISMATCH/NOT_FOUND/BAD_DOI（--strict 时含 UNVERIFIED）；2 用法错。
"""
import argparse
import difflib
import html
import json
import os
import re
import subprocess
import sys
import tempfile
import time
import urllib.parse

CROSSREF = "https://api.crossref.org"
DATACITE = "https://api.datacite.org"
OPENALEX = "https://api.openalex.org"
STATUSES = ("OK", "MISMATCH", "NOT_FOUND", "BAD_DOI", "UNVERIFIED")


def _venv_guard():
    """缺依赖时切到 nanobot venv（本工具只用 stdlib，保留以防将来加依赖）。"""
    try:
        import json  # noqa: F401
    except Exception:
        pass


def curl_json(url, timeout=45):
    """GET 一个 URL 并解析 JSON；返回 (data, http_code, err)。"""
    cmd = ["curl", "-s", "-m", str(timeout), "-w", "\n%{http_code}", url]
    proxy = os.environ.get("http_proxy") or os.environ.get("HTTP_PROXY")
    if proxy:
        cmd[1:1] = ["-x", proxy]
    r = subprocess.run(cmd, capture_output=True, text=True)
    body, _, code = r.stdout.rpartition("\n")
    code = code.strip()
    if not body.strip():
        return None, code, "empty body"
    try:
        return json.loads(body), code, None
    except Exception as e:
        return None, code, f"bad json: {e}"


# ---------- 文本归一化 / 相似度 ----------

def norm(s):
    if not s:
        return ""
    s = html.unescape(re.sub(r"<[^>]+>", " ", s))
    s = s.replace("&amp;", "&")
    s = re.sub(r"[^0-9a-zA-Z\u4e00-\u9fff]+", " ", s.lower())
    s = re.sub(r"^the ", "", s.strip())
    return re.sub(r"\s+", " ", s).strip()


def sim(a, b):
    a, b = norm(a), norm(b)
    if not a or not b:
        return 0.0
    if a == b:
        return 1.0
    # 副标题被截掉的情况：短的是长的前缀，且短的那个本身够长
    short, long = sorted((a, b), key=len)
    if len(short) >= 25 and long.startswith(short) and len(short) / len(long) >= 0.5:
        return 1.0
    return difflib.SequenceMatcher(None, a, b).ratio()


def families(ref):
    out = []
    authors = ref.get("author") or []
    if isinstance(authors, str):
        authors = [p for p in re.split(r",|&|\band\b", authors)]
    for a in authors:
        if isinstance(a, str):
            f = a.strip()
        else:
            f = a.get("family") or a.get("name") or ""
        if f:
            out.append(f.split(",")[0].strip())
    if not out and ref.get("unstructured"):
        head = ref["unstructured"].split("(")[0]
        out = [p.strip() for p in re.split(r"[,&]| and ", head) if len(p.strip()) > 2][:3]
    return out


# ---------- 无 DOI 条目的启发式解析 ----------

VOL_RE = re.compile(r"([A-Za-z&:'\-\s.]{2,120}),\s*(\d{1,3})\s*\(\s*(\d{1,2})\s*\)")
VOL_ONLY_RE = re.compile(r"([A-Za-z&:'\-\s.]{2,120}),\s*(\d{1,3})\s*,\s*\d")


def _journal_tail(span):
    """把「标题句 + 期刊名」那段切成期刊名（取最后一个句号/括号年份之后）。"""
    tail = re.split(r"\.\s+|\(\d{4}[a-z]?\)\.?\s*", span)[-1]
    return tail.strip(" ,.\u2019'")

YEAR_RE = re.compile(r"\((?:19|20)\d{2}[a-z]?\)|\b(19|20)\d{2}\b")
PAGE_RE = re.compile(r"(\d{1,5})\s*[-\u2010-\u2015]\s*(\d{1,5})")
URL_RE = re.compile(r"https?://[^\s)\]>,]+")
STOP = set("the of and for in on a an to with from is are by at as or how what"
           " its their this that not".split())


def title_tokens(claim, n=8):
    head = URL_RE.sub(" ", claim or "")
    toks = [t for t in re.findall(r"[A-Za-z][A-Za-z\-]{3,}", head) if t.lower() not in STOP]
    return toks[:n]


def title_variants(claim):
    """一条引文里可能带着作者前缀 / 刊名尾巴，抠出几种子串一起比。"""
    if not claim:
        return []
    out = [claim]
    body = re.sub(r"^.{0,80}?\((?:19|20)\d{2}[a-z]?\)\.?\s*", "", claim)
    if body != claim:
        out.append(body)
    # 去掉句尾的刊名/出版社段（取第一个句号前的部分）
    head = re.split(r"\.\s", body)[0]
    if head and head not in out:
        out.append(head)
    # 副标题（冒号后）单独比，引文常把副标题截掉
    if ":" in head:
        pre = head.split(":")[0].strip()
        if len(pre) >= 15 and pre not in out:
            out.append(pre)
    return out


def best_sim(claim, target):
    return max((sim(v, target) for v in title_variants(claim)), default=sim(claim, target))


def meta_diff(rec, volume, year):
    """引文自报的卷/年 vs 真实记录的卷/年，不一致就返回说明列表。"""
    notes = []
    rv, mv = str(rec.get("volume") or ""), str(volume or "")
    ryr, myr = str(rec.get("year") or ""), str(year or "")
    if rv and mv and rv != mv:
        notes.append(f"卷 {rv}\u2260{mv}")
    if ryr.isdigit() and myr.isdigit() and abs(int(ryr) - int(myr)) >= 1:
        notes.append(f"年 {ryr}\u2260{myr}")
    return notes


SEARCH_TMPL = [
    "https://{host}/search?keys={q}",      # Drupal（justice.gov 等）
    "https://{host}/?s={q}",               # WordPress
    "https://{host}/search?q={q}",         # 通用
    "https://{host}/en/search?q={q}",
]

ANCHOR_RE = re.compile(r'<a\b[^>]*>(.*?)</a>', re.I | re.S)
TAG_RE = re.compile(r'<[^>]+>')


def _fetch(url, timeout=20):
    """返回 (body, http_code, effective_url)。"""
    cmd = ["curl", "-sL", "-m", str(timeout), "--max-filesize", "4000000",
           "-w", "\n%{http_code}\n%{url_effective}"]
    proxy = os.environ.get("http_proxy") or os.environ.get("HTTP_PROXY")
    if proxy:
        cmd[1:1] = ["-x", proxy]
    r = subprocess.run(cmd + [url], capture_output=True, text=True, errors="replace")
    parts = r.stdout.rsplit("\n", 2)
    if len(parts) != 3:
        return "", "", ""
    body, code, eff = parts
    return body, code.strip(), eff.strip()


def site_search(host, claim):
    """「只给了站点首页」的灰文：用站点自身搜索当站内检索。

    返回 (verdict, detail)。verdict = OK / NOT_FOUND / UNVERIFIED。
    三道闸门，缺一条就不下结论（宁可 UNVERIFIED 也不瞎判）：
      ① 最终 URL 的路径必须与请求路径一致（尾斜杠宽容）—— 被重定向到别的路径
         就是伪搜索引擎（torrentfreak 的 /search → 某篇旧文章的永久链接）。
      ② 搜索页不能与首页正文一样（有些站的 ?s= 直接回首页）。
      ③ 页面上要有一条锚文本与引文标题高度相似；关键词命中不算，因为搜索页
         自己会回显查询词。
    """
    q = urllib.parse.quote(" ".join(title_tokens(claim, 12)))
    root_body, root_code, _ = _fetch(f"https://{host}/")
    root_sig = _sig(root_body) if root_code.startswith("2") else None
    tried = 0
    for tmpl in SEARCH_TMPL:
        url = tmpl.format(host=host, q=q)
        req_path = urllib.parse.urlparse(url).path or "/"
        tried += 1
        body, code, eff = _fetch(url)
        if not code.startswith("2") or len(body) < 200:
            continue
        eff_path = urllib.parse.urlparse(eff).path or "/"
        if eff_path.rstrip("/") != req_path.rstrip("/"):
            continue  # 被重定向到别的路径 = 伪搜索入口
        if root_sig and _same_page(body, root_body):
            continue  # 回的就是首页
        best, best_text = 0.0, ""
        for m in ANCHOR_RE.finditer(body):
            text = html.unescape(TAG_RE.sub(" ", m.group(1))).strip()
            text = re.sub(r"\s+", " ", text)
            if len(text) < 15:
                continue
            s = best_sim(claim, text)
            if s > best:
                best, best_text = s, text
        if best >= 0.70:
            return "OK", f"站内检索命中（相似 {best:.2f}）· {best_text[:70]}"
        # 搜索结果第一页没有，不等于不存在（搜索质量、分页都影响），不判 NOT_FOUND
        return ("UNVERIFIED",
                f"站内检索跑通了但没对上（{tried} 个入口，最高相似 {best:.2f}）")
    return "UNVERIFIED", f"站点不接受站内检索（试了 {tried} 个入口）"


def _sig(body):
    """整页归一化后的长度 + md5（仅供调试对比用）。"""
    import hashlib
    flat = re.sub(r"\s+", " ", body).strip()
    return (len(flat), hashlib.md5(flat.encode("utf-8", "ignore")).hexdigest())


def _same_page(a, b):
    """两页是否「就是同一页」。

    不能用整页 hash，也不能按位置抽样：「搜索结果是 JS 渲染」的站把首页原样
    回给你时，中间插 55 字节就会把后面所有采样点错位（bleepingcomputer 就是）。
    改成词序列的 quick_ratio（基于多重集的快速上界，不跑回溯），≥0.998 算同一页。
    """
    if not a or not b:
        return False
    wa, wb = a.split(), b.split()
    if not wa or not wb:
        return False
    return difflib.SequenceMatcher(None, wa, wb).quick_ratio() >= 0.998


def check_url(url, claim, timeout=25):
    """灰文 / 网站类引用：URL 能不能打开 + 页面上有没有标题关键词。不执行 JS。"""
    p = urllib.parse.urlparse(url)
    if not p.path or p.path in ("/", ""):
        # 只给了首页：先试站内检索，能跑就跑
        verdict, detail = site_search(p.netloc, claim)
        return verdict, f"{detail} · {url[:50]}"
    cmd = ["curl", "-sL", "-m", str(timeout), "-w", "\n%{http_code}"]
    proxy = os.environ.get("http_proxy") or os.environ.get("HTTP_PROXY")
    if proxy:
        cmd[1:1] = ["-x", proxy]
    r = subprocess.run(cmd + [url], capture_output=True, text=True, errors="replace")
    body, _, code = r.stdout.rpartition("\n")
    code = code.strip()
    if code[:1] != "2":
        return "UNVERIFIED", f"URL 打不开（HTTP {code or '?'}）· {url[:60]}"
    toks = title_tokens(claim)
    low = body.lower()
    hit = [t for t in toks if t.lower() in low]
    need = max(2, int(0.5 * len(toks)))
    if len(toks) >= 4 and len(hit) >= need:
        return "OK", f"URL 页面含标题关键词 {len(hit)}/{len(toks)} · {url[:70]}"
    return "UNVERIFIED", (f"URL 可达但页面上找不到足够标题关键词（{len(hit)}/{len(toks)}）"
                          f"· {url[:60]}")


def parse_unstructured(s):
    """从一条自由文本引文里抠 journal / volume / issue / year / pages。"""
    info = {}
    if not s:
        return info
    m = YEAR_RE.search(s)
    if m:
        y = re.search(r"(19|20)\d{2}", m.group(0))
        info["year"] = int(y.group(0)) if y else None
    m = VOL_RE.search(s)
    if m:
        info["container"] = _journal_tail(m.group(1))
        info["volume"] = m.group(2)
        info["issue"] = m.group(3)
    else:
        m = VOL_ONLY_RE.search(s)
        if m:
            info["container"] = _journal_tail(m.group(1))
            info["volume"] = m.group(2)
    # 尾部页码范围（取最后一个 x-y）
    pages = PAGE_RE.findall(s)
    if pages:
        a, b = pages[-1]
        if int(b) >= int(a) and int(b) - int(a) < 2000:
            info["page"] = f"{a}-{b}"
    return info


# ---------- Crossref 查询 ----------

class Checker:
    def __init__(self, delay=0.25, verbose=False):
        self.delay = delay
        self.verbose = verbose
        self._cache = {}

    def _get(self, url):
        if url in self._cache:
            return self._cache[url]
        data, code, err = None, None, None
        for attempt in range(3):
            data, code, err = curl_json(url)
            # 空体 / 429 / 000 多为限流或瞬断，退避重试
            if data is not None or code in ("404", "400", "403", "401"):
                break
            time.sleep(2 * (attempt + 1))
        time.sleep(self.delay)
        self._cache[url] = (data, code, err)
        return data, code, err

    def work(self, doi):
        """ Crossref 优先，404 时回退 DataCite（Zenodo / 机构库等只注册在 DataCite）。"""
        url = f"{CROSSREF}/works/{urllib.parse.quote(doi, safe='')}"
        data, code, err = self._get(url)
        if data is not None and data.get("status") == "ok":
            return data["message"], code, None
        durl = f"{DATACITE}/dois/{urllib.parse.quote(doi, safe='')}"
        ddata, dcode, derr = self._get(durl)
        attrs = D(D(ddata).get("data")).get("attributes") if ddata else None
        if attrs:
            creators = []
            for c in (attrs.get("creators") or [])[:6]:
                fam = c.get("familyName") or (c.get("name") or "").split(",")[0]
                creators.append({"family": fam})
            msg = {
                "title": [t.get("title", "") for t in (attrs.get("titles") or [])],
                "author": creators,
                "container-title": [attrs.get("publisher") or ""],
                "issued": {"date-parts": [[attrs.get("publicationYear")]]},
                "type": D(attrs.get("types")).get("resourceType"),
                "DOI": doi,
                "_source": "datacite",
            }
            return msg, dcode, None
        return None, code, err or (data or {}).get("message") or f"datacite HTTP {dcode}"

    def openalex_source(self, container):
        """用 OpenAlex 判「期刊是否真存在」，返回 (issn 列表, display_name, sim)。"""
        url = (f"{OPENALEX}/sources?search={urllib.parse.quote(container)}"
               f"&per-page=8&select=display_name,issn_l,issn,type")
        data, code, err = self._get(url)
        best = (None, None, 0.0)
        for it in D(data).get("results") or []:
            name = it.get("display_name") or ""
            s = sim(container, name)
            if s > best[2]:
                issns = list(dict.fromkeys([it.get("issn_l")] + list(it.get("issn") or [])))
                best = ([i for i in issns if i], name, s)
        return best

    def search(self, title, authors=None, rows=5):
        q = title[:200]
        if authors:
            q += " " + " ".join(authors[:2])
        url = (f"{CROSSREF}/works?query.bibliographic={urllib.parse.quote(q)}"
               f"&rows={rows}&select=title,DOI,issued,container-title,author,volume,issue,page")
        data, code, err = self._get(url)
        if data is None:
            return []
        return D(data).get("message", {}).get("items", [])

    def resolve_issn(self, container):
        url = (f"{CROSSREF}/works?query.container-title={urllib.parse.quote(container)}"
               f"&rows=5&select=container-title,ISSN")
        data, _, _ = self._get(url)
        items = (data or {}).get("message", {}).get("items", [])
        best, best_score = None, 0.0
        for it in items:
            cand = (it.get("container-title") or [""])[0]
            s = sim(container, cand)
            if s > best_score and it.get("ISSN"):
                best, best_score = list(dict.fromkeys(it["ISSN"])), s
        if best and best_score >= 0.80:
            return best, best_score
        return None, best_score

    def toc(self, issns, year):
        """拉某刊若干年的条目（Crossref 的 from-pub-date 是上网日期，期刊卷期年份
        常差 1–2 年，所以窗口前后各开 2 年，靠卷期号筛）；最多 5 页 × 1000。"""
        if isinstance(issns, str):
            issns = [issns]
        lo, hi = int(year) - 2, int(year) + 2
        last_err = "没有 ISSN"
        for issn in issns:
            for route in (
                f"{CROSSREF}/works?filter=issn:{issn},from-pub-date:{lo}-01-01,"
                f"until-pub-date:{hi}-12-31&rows=1000&cursor=*",
                f"{CROSSREF}/journals/{issn}/works?filter=from-pub-date:{lo}-01-01,"
                f"until-pub-date:{hi}-12-31&rows=1000&cursor=*",
            ):
                items, err = self._paginate(route)
                if items:
                    return items, err, issn
                last_err = err or last_err
        return [], last_err, None

    def _paginate(self, url):
        out = []
        for _ in range(5):
            data, code, err = self._get(url)
            if data is None:
                return out, err or f"HTTP {code}"
            msg = data.get("message", {})
            out.extend(msg.get("items", []))
            cursor = msg.get("next-cursor")
            if not cursor or len(msg.get("items", [])) < 1000:
                break
            url = re.sub(r"cursor=[^&]*", "cursor=" + urllib.parse.quote(cursor), url)
        return out, None


def D(x):
    return x if isinstance(x, dict) else {}


# ---------- 单条判定 ----------

def check_one(ck, n, ref):
    raw = ref
    title = (ref.get("article-title") or "").strip()
    unstructured = (ref.get("unstructured") or "").strip()
    claim = title or unstructured
    rec = {"n": n, "claim": claim, "doi": ref.get("DOI")}
    parsed = parse_unstructured(unstructured) if unstructured else {}
    for k in ("container", "volume", "issue", "year", "page"):
        v = ref.get("journal-title") if k == "container" else ref.get(k if k != "year" else "year")
        if k == "container":
            v = ref.get("journal-title") or parsed.get("container")
        elif k == "page":
            v = ref.get("first-page") or parsed.get("page")
        rec[k] = v or parsed.get(k)

    # ① 带 DOI：直接查
    if ref.get("DOI"):
        m, code, err = ck.work(ref["DOI"])
        if m is None:
            rec["status"] = "BAD_DOI"
            rec["detail"] = f"DOI 打不开（HTTP {code}）{('· ' + str(err))[:60] if err else ''}"
            return rec
        real_title = (m.get("title") or [""])[0]
        rec["real_title"] = real_title
        rec["real_doi"] = m.get("DOI")
        rec["real_container"] = (m.get("container-title") or [""])[0]
        rec["real_year"] = D(m.get("issued")).get("date-parts", [[None]])[0][0]
        rec["real_authors"] = [a.get("family", "") for a in m.get("author", [])[:4]]
        score = best_sim(claim, real_title) if claim else 1.0
        ref_fam = [f.lower() for f in families(ref)]
        hit_author = any(f in [x.lower() for x in rec["real_authors"]] for f in ref_fam) if ref_fam else None
        rec["title_sim"] = round(score, 3)
        if score >= 0.86 or (score >= 0.6 and hit_author):
            # 标题对得上但卷期/年份明显不对 → 元数据错
            diff_notes = meta_diff(rec, m.get("volume"), rec["real_year"])
            if diff_notes:
                rec["status"] = "MISMATCH"
                rec["detail"] = (f"记录存在但元数据错（{'，'.join(diff_notes)}）："
                                 f"{real_title[:60]} · {rec['real_container'][:30]}")
                return rec
            rec["status"] = "OK"
            rec["detail"] = f"{rec['real_year']} · {real_title[:80]} · {rec['real_container'][:40]}"
        else:
            rec["status"] = "MISMATCH"
            rec["detail"] = (f"DOI 指向的是「{real_title[:70]}」({rec['real_year']}, "
                             f"{rec['real_container'][:30]})，与引文对不上")
        return rec

    # ② 无 DOI：先用标题反查真实记录
    candidates = ck.search(claim or "", families(ref))
    best, best_score = None, 0.0
    for it in candidates:
        t = (it.get("title") or [""])[0]
        s = best_sim(claim, t) if claim else 0.0
        if s > best_score:
            best, best_score = it, s
    rec["search_best"] = round(best_score, 3)
    if best and best_score >= 0.90:
        rec["real_title"] = (best.get("title") or [""])[0]
        rec["real_doi"] = best.get("DOI")
        rec["real_container"] = (best.get("container-title") or [""])[0]
        rec["real_year"] = D(best.get("issued")).get("date-parts", [[None]])[0][0]
        rec["real_authors"] = [a.get("family", "") for a in best.get("author", [])[:4]]
        notes = meta_diff(rec, best.get("volume"), rec["real_year"])
        if notes:
            rec["status"] = "MISMATCH"
            rec["detail"] = (f"找到「{rec['real_title'][:60]}」({rec['real_doi']})，"
                             f"但{'，'.join(notes)}")
            return rec
        rec["status"] = "OK"
        rec["detail"] = (f"{rec['real_year']} · {rec['real_title'][:70]} · "
                         f"{rec['real_container'][:35]} · {rec['real_doi']}")
        return rec

    # ③ 期刊目录（TOC）比对
    container, year = rec.get("container"), rec.get("year")
    if container and year:
        issns, iscore = ck.resolve_issn(container)
        if not issns:
            oa_issns, oa_name, oa_score = ck.openalex_source(container)
            if oa_issns and oa_score >= 0.90:
                issns, iscore = oa_issns, oa_score
                rec["issn_source"] = f"openalex:{oa_name}"
            elif oa_name and oa_score >= 0.90:
                rec["status"] = "UNVERIFIED"
                rec["detail"] = f"期刊名匹配到「{oa_name}」但拿不到 ISSN，无法拉目录"
                return rec
            else:
                near = f"最接近「{oa_name}」相似 {oa_score:.2f}" if oa_name else "OpenAlex 无相近刊名"
                if oa_score < 0.65:
                    rec["status"] = "NOT_FOUND"
                    rec["detail"] = f"期刊「{container}」不存在（{near}）"
                else:
                    rec["status"] = "UNVERIFIED"
                    rec["detail"] = f"期刊「{container}」查不到 ISSN（{near}）"
                return rec
        items, err, used_issn = ck.toc(issns, year)
        if err or not items:
            rec["status"] = "UNVERIFIED"
            rec["detail"] = f"拉 {container} {year} 年目录失败（{err or '空'}）"
            return rec
        rec["issn"] = used_issn
        vol, iss = str(rec.get("volume") or ""), str(rec.get("issue") or "")
        label = f"{vol}({iss})" if vol and iss else (vol or f"{year} 年")
        scoped = [it for it in items if str(it.get("volume") or "") == vol] if vol else []
        if iss:
            exact = [it for it in scoped if str(it.get("issue") or "") == iss]
            scoped = exact or scoped
        pool = scoped or items
        best_item, best_s = None, 0.0
        for it in pool:
            s = best_sim(claim, (it.get("title") or [""])[0]) if claim else 0.0
            if s > best_s:
                best_item, best_s = it, s
        rec["toc_size"] = len(pool)
        rec["toc_best"] = round(best_s, 3)
        if len(pool) < 5:
            rec["status"] = "UNVERIFIED"
            rec["detail"] = (f"{container} {year} 在 Crossref 只有 {len(pool)} 条，"
                             f"目录不全，判不了（最高相似 {best_s:.2f}）")
            return rec
        if best_item and best_s >= 0.85:
            rec["status"] = "OK"
            rec["real_title"] = (best_item.get("title") or [""])[0]
            rec["real_doi"] = best_item.get("DOI")
            rec["real_year"] = D(best_item.get("issued")).get("date-parts", [[None]])[0][0]
            rec["real_authors"] = [a.get("family", "") for a in best_item.get("author", [])[:4]]
            rec["detail"] = (f"{label} 目录命中 {rec['real_year']} · "
                             f"{rec['real_title'][:70]} · {rec['real_doi']}")
            return rec
        # 首页被别的文章占了 → 卷期页码安错
        page = str(rec.get("page") or "")
        first = page.split("-")[0] if page else ""
        if first and scoped:
            for it in scoped:
                rp = str(it.get("page") or "")
                if rp and rp.split("-")[0] == first:
                    rec["status"] = "MISMATCH"
                    rec["real_title"] = (it.get("title") or [""])[0]
                    rec["real_doi"] = it.get("DOI")
                    rec["detail"] = (f"{label} 第 {first} 页是「{rec['real_title'][:60]}」，"
                                     f"不是引文这篇")
                    return rec
        rec["status"] = "NOT_FOUND"
        where = f"{container} {year}" + (f" {label}" if vol else "")
        rec["detail"] = (f"{where} 目录里没有这篇（{len(pool)} 条已比，最高相似 {best_s:.2f}）")
        return rec

    # ④ 灰文 / 网站类：看 URL 能不能开、页面上有没有标题关键词
    found = URL_RE.search(unstructured)
    url = ref.get("URL") or (found.group(0) if found else "")
    if url:
        rec["status"], rec["detail"] = check_url(url, claim)
        return rec

    # ⑤ 什么抓手都没有
    rec["status"] = "UNVERIFIED"
    rec["detail"] = "无 DOI、无期刊卷期、无 URL，机器判不了"
    return rec


# ---------- 入口 ----------

def load_refs(args):
    if args.refs:
        with open(args.refs) as f:
            data = json.load(f)
        if isinstance(data, dict):
            data = data.get("reference") or data.get("refs") or []
        return data
    m, code, err = curl_json(f"{CROSSREF}/works/{urllib.parse.quote(args.doi, safe='')}")
    if not m or m.get("status") != "ok":
        sys.exit(f"拉不到 {args.doi} 的元数据（HTTP {code} {err or ''}）")
    msg = m["message"]
    refs = msg.get("reference") or []
    if args.verbose:
        print(f"# {args.doi} → {(msg.get('title') or [''])[0][:70]}")
        print(f"# deposited reference list: {len(refs)} 条")
    return refs


def main():
    ap = argparse.ArgumentParser(description="机器核对论文参考文献（Crossref）")
    src = ap.add_mutually_exclusive_group(required=True)
    src.add_argument("--doi", help="被查论文的 DOI（用它的 deposited reference list）")
    src.add_argument("--refs", help="JSON 文件：Crossref reference 结构数组")
    ap.add_argument("--json", action="store_true", help="输出 JSON")
    ap.add_argument("-o", "--out", help="把完整结果写成 JSON 文件")
    ap.add_argument("--delay", type=float, default=0.25, help="每次请求后的间隔秒数（默认 0.25）")
    ap.add_argument("--limit", type=int, default=0, help="只核前 N 条（调试用）")
    ap.add_argument("--only", type=int, default=0, help="只核第 N 条（调试用，从 1 起）")
    ap.add_argument("--strict", action="store_true", help="UNVERIFIED 也算失败")
    ap.add_argument("-v", "--verbose", action="store_true")
    args = ap.parse_args()

    refs = load_refs(args)
    offset = 0
    if args.only:
        if not 1 <= args.only <= len(refs):
            sys.exit(f"--only {args.only} 超范围（共 {len(refs)} 条）")
        offset = args.only - 1
        refs = refs[offset:offset + 1]
    elif args.limit:
        refs = refs[:args.limit]
    if not refs:
        sys.exit("没有参考文献可核（reference list 为空或全缺字段）")

    ck = Checker(delay=args.delay, verbose=args.verbose)
    results = []
    for i, r in enumerate(refs, 1):
        res = check_one(ck, i + offset, r)
        results.append(res)
        if not args.json:
            print(f'{res["n"]:3d} {res["status"]:<10} {res.get("detail", "")[:150]}')

    counts = {s: 0 for s in STATUSES}
    for r in results:
        counts[r["status"]] = counts.get(r["status"], 0) + 1

    if args.out:
        json.dump({"source": args.doi or args.refs, "counts": counts, "results": results},
                  open(args.out, "w"), ensure_ascii=False, indent=1)
    if args.json:
        print(json.dumps({"counts": counts, "results": results}, ensure_ascii=False, indent=1))
    else:
        total = len(results)
        bad = counts["MISMATCH"] + counts["NOT_FOUND"] + counts["BAD_DOI"]
        print("\n" + " · ".join(f"{k} {v}" for k, v in counts.items() if v)
              + f"  （共 {total} 条，有问题 {bad} 条）")

    bad = counts["MISMATCH"] + counts["NOT_FOUND"] + counts["BAD_DOI"]
    if bad or (args.strict and counts["UNVERIFIED"]):
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
