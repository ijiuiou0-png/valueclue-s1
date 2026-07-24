#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
fetch_disclosures.py — 一手法定披露每日管道（S1 中的 S1）
三路官方源：美股 SEC EDGAR / A股 巨潮资讯 / 港股 HKEX 披露易
用法:
  python3 fetch_disclosures.py --build-map          # 一次性构建/增量补 ID 映射(CIK/orgId/stockId)
  python3 fetch_disclosures.py [--days 1]           # 拉取近 N 天披露 → disclosures/YYYY-MM-DD.jsonl
标的清单来源: watchlist-disclosure.json (ticker/market/name)；缺映射的自动解析并缓存进 id_map.json
纪律: 只拉元数据(日期/类型/标题/链接)，PDF 按需人工/agent 再取；全部直连不走代理。
"""
import json, sys, time, datetime, argparse, urllib.request, urllib.parse, os

BASE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(BASE)
WL = os.path.join(BASE, "watchlist-disclosure.json")
IDMAP = os.path.join(BASE, "id_map.json")
OUTDIR = os.path.join(ROOT, "disclosures")
UA = {"User-Agent": "ValueClue Research ${SEC_CONTACT_EMAIL}"}
UA_WEB = {"User-Agent": "Mozilla/5.0 (Macintosh) AppleWebKit/537.36"}

# 海外源加速代理(nas家宽→server-b出海): env OVERSEA_PROXY=http://${SERVER_TS_IP}:8888
# 铁律: 国内源(巨潮等)永远直连,不进 OVERSEA_HOSTS; 代理失败最后一次尝试自动降级直连
OVERSEA_PROXY = os.environ.get("OVERSEA_PROXY", "")
OVERSEA_HOSTS = ("sec.gov", "hkexnews.hk")
_PROXY_OPENER = urllib.request.build_opener(urllib.request.ProxyHandler(
    {"http": OVERSEA_PROXY, "https": OVERSEA_PROXY})) if OVERSEA_PROXY else None

def openurl(req, timeout, att=0, retries=0):
    """att<retries 且是海外域名→走代理; 最后一次尝试降级直连"""
    url = req.full_url if hasattr(req, "full_url") else str(req)
    if _PROXY_OPENER and any(h in url for h in OVERSEA_HOSTS) and att < max(retries, 1):
        return _PROXY_OPENER.open(req, timeout=timeout)
    return urllib.request.urlopen(req, timeout=timeout)

def http(url, data=None, headers=None, timeout=15, retries=2):
    for att in range(retries + 1):
        try:
            req = urllib.request.Request(url, data=data.encode() if isinstance(data, str) else data,
                                         headers=headers or UA_WEB)
            with openurl(req, timeout, att, retries) as r:
                return r.read().decode("utf-8", "ignore")
        except Exception:
            if att == retries: raise
            time.sleep(2 * (att + 1))

def load(p, default):
    try:
        return json.load(open(p, encoding="utf-8"))
    except Exception:
        return default

# ---------- ID 映射构建 ----------
def build_map():
    wl = load(WL, [])
    idmap = load(IDMAP, {})
    # 美股: SEC 官方全量 ticker→CIK 表(一次拉)
    sec_map = None
    for it in wl:
        t, mkt = it["ticker"], it["market"]
        if t in idmap: continue
        try:
            if mkt == "US":
                if sec_map is None:
                    sec_map = {v["ticker"]: str(v["cik_str"]).zfill(10)
                               for v in json.loads(http("https://www.sec.gov/files/company_tickers.json", headers=UA)).values()}
                if t in sec_map:
                    idmap[t] = {"market": "US", "cik": sec_map[t]}
            elif mkt == "CN":
                r = json.loads(http("http://www.cninfo.com.cn/new/information/topSearch/detailOfQuery",
                                    data="keyWord=%s&maxSecNum=3" % t, headers={**UA_WEB, "Content-Type": "application/x-www-form-urlencoded"}))
                for k in r.get("keyBoardList", []):
                    if k["code"] == t:
                        idmap[t] = {"market": "CN", "orgId": k["orgId"], "plate": k.get("plate", "sse"), "name": k.get("zwjc", "")}
                        break
            elif mkt == "HK":
                code5 = t.zfill(5)
                r = http("https://www1.hkexnews.hk/search/prefix.do?callback=x&lang=ZH&type=A&name=%s&market=SEHK" % code5, headers=UA_WEB)
                j = json.loads(r[r.index("(")+1:r.rindex(")")])
                for rec in j.get("stockInfo", []):
                    if rec["code"].lstrip("0") == t.lstrip("0"):
                        idmap[t] = {"market": "HK", "stockId": rec["stockId"], "name": rec.get("name", "")}
                        break
            time.sleep(0.4)
            print("mapped:", t, idmap.get(t, "FAILED"))
        except Exception as e:
            print("map-fail:", t, e)
    json.dump(idmap, open(IDMAP, "w", encoding="utf-8"), ensure_ascii=False, indent=1)
    print("id_map: %d/%d" % (len(idmap), len(wl)))

# ---------- 三路拉取 ----------
def fetch_us_efts(cik, since, until):
    """SEC EDGAR Full-Text Search：补抓 8-K/10-Q，按 accession 去重。"""
    params = urllib.parse.urlencode({
        "q": "", "dateRange": "custom", "startdt": since, "enddt": until,
        "forms": "8-K,10-Q", "ciks": str(cik).zfill(10), "from": 0, "size": 100,
    })
    d = json.loads(http(
        "https://efts.sec.gov/LATEST/search-index?" + params,
        headers={**UA, "Accept": "application/json"},
    ))
    out = []
    for hit in d.get("hits", {}).get("hits", []):
        src = hit.get("_source", {})
        accession = src.get("adsh") or str(hit.get("_id", "")).split(":", 1)[0]
        document = str(hit.get("_id", "")).split(":", 1)[1] if ":" in str(hit.get("_id", "")) else ""
        form = src.get("form") or ((src.get("root_forms") or [""])[0])
        if not accession or not document or form not in ("8-K", "10-Q"):
            continue
        out.append({
            "form": form, "date": src.get("file_date", ""),
            "title": src.get("file_description") or form,
            "url": "https://www.sec.gov/Archives/edgar/data/%s/%s/%s" % (
                int(cik), accession.replace("-", ""), document,
            ),
            "accession": accession, "source_api": "SEC EDGAR Full-Text Search",
        })
    return out


def fetch_us(cik, since, until=None):
    out = []
    d = json.loads(http("https://data.sec.gov/submissions/CIK%s.json" % cik, headers=UA))
    r = d["filings"]["recent"]
    for i in range(len(r["form"])):
        if r["filingDate"][i] < since: break
        out.append({"form": r["form"][i], "date": r["filingDate"][i],
                    "title": r["primaryDocDescription"][i] or r["form"][i],
                    "url": "https://www.sec.gov/Archives/edgar/data/%s/%s/%s" % (int(cik), r["accessionNumber"][i].replace("-", ""), r["primaryDocument"][i]),
                    "accession": r["accessionNumber"][i], "source_api": "SEC Submissions API"})
    if until:
        try:
            out.extend(fetch_us_efts(cik, since, until))
        except Exception as e:
            print("efts-fail:", cik, e, file=sys.stderr)
    dedup = {}
    for row in out:
        dedup[(row.get("accession"), row.get("form"), row.get("date"))] = row
    return list(dedup.values())

def fetch_cn_tab(code, orgid, plate, since, until, tab_name="fulltext"):
    """巨潮普通公告/调研专栏分页抓取；relation 覆盖投资者关系活动记录表。"""
    out = []
    page = 1
    while True:
        payload = urllib.parse.urlencode({
            "pageNum": page, "pageSize": 30, "column": plate,
            "tabName": tab_name, "plate": "", "stock": "%s,%s" % (code, orgid),
            "searchkey": "", "secid": "", "category": "", "trade": "",
            "seDate": "%s~%s" % (since, until), "sortName": "", "sortType": "",
            "isHLtitle": "true",
        })
        d = json.loads(http(
            "http://www.cninfo.com.cn/new/hisAnnouncement/query",
            data=payload,
            headers={**UA_WEB, "Content-Type": "application/x-www-form-urlencoded"},
        ))
        anns = d.get("announcements") or []
        for a in anns:
            ts = datetime.datetime.fromtimestamp(a["announcementTime"] / 1000).strftime("%Y-%m-%d")
            form = a.get("announcementTypeName") or (
                "投资者关系活动记录表" if tab_name == "relation" else "公告"
            )
            out.append({"form": form, "date": ts,
                        "title": a["announcementTitle"],
                        "url": "http://static.cninfo.com.cn/" + a["adjunctUrl"]})
        total = int(d.get("totalAnnouncement") or len(anns))
        if not anns or page * 30 >= total:
            break
        page += 1
        time.sleep(0.4)  # 巨潮频率限制：逐页最多约 2.5 req/s
    return out


def fetch_cn(code, orgid, plate, since, until):
    rows = (
        fetch_cn_tab(code, orgid, plate, since, until, "fulltext")
        + fetch_cn_tab(code, orgid, plate, since, until, "relation")
    )
    dedup = {}
    for row in rows:
        dedup[(row.get("date"), row.get("title"), row.get("url"))] = row
    return list(dedup.values())

def fetch_hk(stockid, since, until):
    out = []
    u = ("https://www1.hkexnews.hk/search/titleSearchServlet.do?sortDir=0&sortByOptions=DateTime&category=0&market=SEHK"
         "&stockId=%s&documentType=-1&fromDate=%s&toDate=%s&searchType=1&t1code=-2&t2Gcode=-2&t2code=-2&rowRange=30&lang=zh" % (stockid, since, until))
    d = json.loads(http(u, headers=UA_WEB))
    recs = json.loads(d["result"]) if isinstance(d.get("result"), str) else d.get("result", [])
    for r in recs:
        dt = r.get("DATE_TIME", "")[:10]
        if "/" in dt:
            try:
                d_, m_, y_ = dt.split("/"); dt = "%s-%s-%s" % (y_, m_, d_)
            except Exception: dt = dt.replace("/", "-")
        out.append({"form": r.get("LONG_TEXT", "公告")[:20], "date": dt,
                    "title": r.get("TITLE", ""),
                    "url": "https://www1.hkexnews.hk" + r.get("FILE_LINK", "") if r.get("FILE_LINK") else ""})
    return out


# ---------- 分级打标 ----------
FIN_US = {"10-K", "10-Q", "20-F", "6-K", "8-K"}  # 8-K 细分看 item,先按重大处理
MAJOR_KW = ["配售", "增发", "定增", "回购", "收购", "重组", "减持", "增持", "中标", "股权激励授予",
            "业绩预告", "业绩快报", "利润分配", "分拆", "退市", "问询", "处罚", "诉讼",
            "placing", "rights issue", "buy-back", "acquisition", "merger"]
FIN_KW = ["年度报告", "年报", "半年度报告", "半年报", "季度报告", "一季报", "三季报",
          "业绩公告", "業績公告", "中期業績", "全年業績", "annual report", "interim report"]
MEET_KW = ["投资者关系活动", "调研", "业绩说明会", "路演", "股东大会", "股東週年大會", "股東特別大會"]

def classify(market, form, title):
    tl = (title or "").lower()
    if market == "US":
        # SEC 修订财报以 10-K/A、10-Q/A、20-F/A 命名，也必须进入财报链。
        fu = (form or "").upper()
        if fu in ("10-K", "10-Q", "20-F", "10-K/A", "10-Q/A", "20-F/A"): return "财报"
        if form == "8-K": return "重大"
        if form in ("SC 13D", "SC 13G", "13D", "13G", "4"): return "重大" if form != "4" else "常规"
        if form == "6-K": return "重大"
        return "常规"
    for k in FIN_KW:
        if k.lower() in tl: return "财报"
    for k in MEET_KW:
        if k in (title or ""): return "会议"
    for k in MAJOR_KW:
        if k.lower() in tl: return "重大"
    return "常规"

def download_pdf(row, base):
    """财报/重大/会议 → 下载原文到 pdf/{ticker}/"""
    if row["tier"] not in ("财报", "重大", "会议") or not row.get("url"): return ""
    import hashlib, re as _re
    d = os.path.join(base, "pdf", row["ticker"])
    os.makedirs(d, exist_ok=True)
    safe = _re.sub(r"[^\w\u4e00-\u9fff-]", "_", (row["title"] or "doc"))[:60]
    fn = os.path.join(d, "%s_%s_%s%s" % (row["date"], row["tier"], safe, ".pdf" if ".pdf" in row["url"].lower() else ".html"))
    if os.path.exists(fn): return fn
    for att in range(2):
        try:
            req = urllib.request.Request(row["url"], headers=UA_WEB if row["market"] != "US" else UA)
            with openurl(req, 30, att, 1) as r, open(fn, "wb") as f:
                f.write(r.read())
            return fn
        except Exception as e:
            if att: print("pdf-fail:", row["ticker"], e, file=sys.stderr)
    return ""

def run(days):
    wl, idmap = load(WL, []), load(IDMAP, {})
    today = datetime.date.today()
    since = (today - datetime.timedelta(days=days)).isoformat()
    os.makedirs(OUTDIR, exist_ok=True)
    outfile = os.path.join(OUTDIR, "%s.jsonl" % today.isoformat())
    n = 0
    with open(outfile, "w", encoding="utf-8") as f:
        for it in wl:
            t, m = it["ticker"], it["market"]
            ids = idmap.get(t)
            if not ids:
                continue
            try:
                if m == "US":
                    rows = fetch_us(ids["cik"], since, today.isoformat())
                elif m == "CN":
                    rows = fetch_cn(t, ids["orgId"], ids.get("plate", "sse"), since, today.isoformat())
                elif m == "HK":
                    rows = fetch_hk(ids["stockId"], since.replace("-", ""), today.isoformat().replace("-", ""))
                else:
                    rows = []
                for r in rows:
                    r.update({"ticker": t, "market": m, "name": it.get("name", "")})
                    r["tier"] = classify(m, r.get("form", ""), r.get("title", ""))
                    lp = download_pdf(r, OUTDIR)
                    if lp: r["local"] = os.path.relpath(lp, OUTDIR)
                    f.write(json.dumps(r, ensure_ascii=False) + "\n"); n += 1
                time.sleep(0.5)
            except Exception as e:
                print("fetch-fail:", t, e, file=sys.stderr)
    print("disclosures: %d 条 → %s" % (n, outfile))

if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--build-map", action="store_true")
    ap.add_argument("--days", type=int, default=1)
    a = ap.parse_args()
    build_map() if a.build_map else run(a.days)
