#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""gen_s1feed.py v3 — S1 情报流数据聚合器
v3(2026-07-14): 自动涌现升级为90日新词雷达，保留首现/首发者/来源扩散路径；涌现分仍为频次×跨公司数。
v2(2026-07-12 CEO四条意见): ①全量披露(供按公司搜索) ②财报按公司分组 ③三组趋势词(玲姐尺/前哨尺/自动涌现·7天序列) ④告警+日历
运行: server-b ~/disclosure-pipeline/ 随 runner 每日跑
输出: /var/www/html/ai-map/s1data/feed.json
"""
import json, os, datetime, collections, glob, statistics

BASE = os.environ.get("S1_BASE", os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
SCRIPTS = os.path.dirname(os.path.abspath(__file__))
OUT_DIR = os.environ.get("S1_OUT_DIR", "/var/www/html/ai-map/s1data")
TODAY = datetime.date.today()
DAYS = 7

def load_jsonl(p):
    try:
        return [json.loads(l) for l in open(p, encoding="utf-8") if l.strip()]
    except Exception:
        return []

# 趋势词库(玲姐尺/前哨尺)
try:
    LEX = json.load(open(os.path.join(SCRIPTS, "trend_words.json"), encoding="utf-8"))
except Exception:
    LEX = {"玲姐尺": [], "前哨尺": []}

feed = {"generated": datetime.datetime.now().strftime("%Y-%m-%d %H:%M"), "days": [], "all": []}
date_list = []
trend = {g: {w: [0]*DAYS for w in LEX.get(g, [])} for g in ("玲姐尺", "前哨尺")}

for i in range(DAYS-1, -1, -1):  # 旧→新,趋势序列左旧右新
    d = (TODAY - datetime.timedelta(days=i)).isoformat()
    date_list.append(d)
    di = DAYS-1-i
    rows = load_jsonl(os.path.join(BASE, "disclosures", "%s.jsonl" % d))
    sigs = load_jsonl(os.path.join(BASE, "signals", "%s.jsonl" % d))
    alerts = load_jsonl(os.path.join(BASE, "alerts", "%s.jsonl" % d))
    # 趋势词计数(标题+信号note)
    text = " ".join(r.get("title","") for r in rows) + " " + " ".join(
        s.get("note", "") + s.get("type", "") + " ".join(s.get("keywords", [])) for s in sigs)
    for g in trend:
        for w in trend[g]:
            c = text.count(w)
            if c: trend[g][w][di] = c
    if not rows and not sigs:
        continue
    tiers = collections.Counter(r.get("tier","常规") for r in rows)
    feed["days"].append({"date": d, "total": len(rows), "tiers": dict(tiers),
                         "signals": sigs, "alerts": alerts})
    # 全量(供搜索,常规也带)
    for r in rows:
        feed["all"].append({"d": d, "m": r["market"], "n": r["name"] or r["ticker"], "tk": r["ticker"],
                            "tier": r.get("tier","常规"), "t": r["title"][:100], "url": r.get("url","")})

feed["dates"] = date_list

# 财报按公司分组
fin = collections.defaultdict(list)
for r in feed["all"]:
    if r["tier"] == "财报":
        fin[(r["n"], r["tk"], r["m"])].append({"d": r["d"], "t": r["t"], "url": r["url"]})
feed["fin_by_co"] = [{"n": k[0], "tk": k[1], "m": k[2], "items": v}
                     for k, v in sorted(fin.items(), key=lambda x: -len(x[1]))]

# 历史财报数据库：逐公司聚合底稿库中的全部定期报告。首页只传目录与最新一份，
# 具体文件仍从 co/<ticker>.json 进入，避免把 1,000+ 条原文塞进首屏。
FIN_RECENT_N = 80
_fr, _fin_seen, _fin_catalog, _fin_dates = [], set(), {}, collections.defaultdict(set)
for _p in glob.glob(os.path.join(BASE, "by_company", "*.jsonl")):
    for _r in load_jsonl(_p):
        if _r.get("tier") != "财报":
            continue
        _row = {"d": _r.get("date", ""), "m": _r.get("market", ""),
                "n": _r.get("name") or _r.get("ticker", ""), "tk": _r.get("ticker", ""),
                "t": (_r.get("title") or _r.get("form") or "")[:100],
                "url": _r.get("url", "")}
        _key = (_row["tk"], _row["url"]) if _row["url"] else (_row["tk"], _row["d"], _row["t"])
        if _key in _fin_seen:
            continue
        _fin_seen.add(_key)
        _fr.append(_row)
        if _row["d"]:
            _fin_dates[_row["tk"]].add(_row["d"])
        _cat = _fin_catalog.setdefault(_row["tk"], {"n": _row["n"], "tk": _row["tk"],
                                                     "m": _row["m"], "count": 0, "latest": {}})
        _cat["count"] += 1
        if not _cat["latest"] or _row["d"] > _cat["latest"].get("d", ""):
            _cat["latest"] = {"d": _row["d"], "t": _row["t"], "url": _row["url"]}
_fr.sort(key=lambda x: (x["d"], x["tk"]), reverse=True)
feed["fin_recent"] = _fr[:FIN_RECENT_N]
feed["fin_total"] = len(_fr)
feed["fin_catalog"] = sorted(_fin_catalog.values(),
                              key=lambda x: (x["m"], -x["count"], x["n"]))
feed["fin_catalog_summary"] = {
    "companies": len(_fin_catalog), "documents": len(_fr),
    "markets": dict(collections.Counter(x["m"] for x in _fin_catalog.values())),
}

# 解读时间轴：同一财报优先采用结构化 v2 解读，旧纯文本记录保留作审计，
# 但不再在前端重复展示。
_brief_rows = load_jsonl(os.path.join(BASE, "briefs", "briefs.jsonl"))
_brief_rows.sort(key=lambda r: (r.get("d", ""), r.get("gen_at", ""),
                                int(r.get("analysis_schema", 0))), reverse=True)
_brief_map = {}
for _r in _brief_rows:
    _key = (_r.get("d", ""), _r.get("tk", ""), _r.get("t", ""), _r.get("type", ""))
    if _key not in _brief_map or int(_r.get("analysis_schema", 0)) > int(_brief_map[_key].get("analysis_schema", 0)):
        _brief_map[_key] = _r
_briefs = sorted(_brief_map.values(), key=lambda r: (r.get("d", ""), r.get("gen_at", "")), reverse=True)
feed["briefs"] = _briefs[:400]
feed["briefs_total"] = len(_briefs)

# CEO/大会视频逐字稿(M1 YouTube腿·经nas; 2026-07-23)
_yt = []
for _p in sorted(glob.glob(os.path.join(BASE, "yt", "*.md")), reverse=True)[:30]:
    try:
        _txt = open(_p, encoding="utf-8").read()
    except Exception:
        continue
    _lines = _txt.split(chr(10))
    _title = _lines[0].lstrip("# ").strip() if _lines else ""
    _meta = {}
    for _l in _lines[1:8]:
        if "频道:" in _l: _meta["ch"] = _l.split("频道:")[1].split("(")[0].strip()
        if "链接:" in _l: _meta["url"] = _l.split("链接:")[1].strip()
        if "日期:" in _l: _meta["date"] = _l.split("日期:")[1].split("·")[0].strip()
    _body = _txt.split("---", 1)[1].strip() if "---" in _txt else ""
    _yt.append({"t": _title, "ch": _meta.get("ch", ""), "d": _meta.get("date", ""),
                "url": _meta.get("url", ""), "chars": len(_body), "ex": _body[:600]})
feed["yt_talks"] = _yt

# 趋势词输出(只留出现过的,按总次数排)
feed["trends"] = {}
for g in trend:
    arr = [{"w": w, "seq": seq, "total": sum(seq)} for w, seq in trend[g].items() if sum(seq) > 0]
    feed["trends"][g] = sorted(arr, key=lambda x: -x["total"])[:15]

# 自动涌现新词雷达(滚动90日；旧周文件无事件明细时保留聚合计数，不伪造首发者)
KW_BLACK = {"配售","回购","增发","可转债","可换股","可转换","定增","股权激励","募投","董事","监事","股东大会","股东会","诉讼","H股","A类股份","分红","利润分配","质押","一般授权","集中竞价","翌日披露","股份发行","配套募资","经营数据","购买资产"}
HOT_DAYS = 90
hot_start = TODAY - datetime.timedelta(days=HOT_DAYS - 1)
week_dates, cursor = [], hot_start
while cursor <= TODAY:
    week = cursor.strftime("%G-W%V")
    if week not in week_dates:
        week_dates.append(week)
    cursor += datetime.timedelta(days=1)
hot = {}
for week in week_dates:
    try:
        for k, v in json.load(open(os.path.join(BASE, "hotwords", "%s.json" % week), encoding="utf-8")).items():
            e = hot.setdefault(k, {"count": 0, "companies": set(), "events": [], "weekly": {}})
            e["count"] += int(v.get("count", 0))
            e["companies"] |= set(v.get("companies", []))
            e["weekly"][week] = int(v.get("count", 0))
            for ev in v.get("events", []):
                if ev.get("date", "") >= hot_start.isoformat():
                    e["events"].append(ev)
    except Exception:
        pass

try:
    wl_for_names = json.load(open(os.path.join(SCRIPTS, "watchlist-disclosure.json"), encoding="utf-8"))
except Exception:
    wl_for_names = []
name_by_ticker = {str(w.get("ticker", "")): w.get("name", "") for w in wl_for_names}
CAPITAL_TYPES = {"扩产", "大额订单", "融资稀释", "回购增持", "并购重组"}
hot_rows = []
for k, v in hot.items():
    if any(b in k for b in KW_BLACK):
        continue
    # 跨周/重跑去重事件；旧数据只有聚合计数时不反推来源。
    event_map = {}
    for ev in v["events"]:
        eid = "|".join([ev.get("date", ""), ev.get("ticker", ""), ev.get("url", ""), k])
        event_map[eid] = ev
    events = sorted(event_map.values(), key=lambda x: (x.get("date", ""), x.get("ticker", "")))
    companies = set(v["companies"]) | {e.get("ticker", "") for e in events if e.get("ticker")}
    count = int(v["count"])
    score = count * len(companies)
    first = events[0] if events else {}
    source_first = {}
    for ev in events:
        source_first.setdefault(ev.get("source", "公告"), ev.get("date", ""))
    source_path = [x[0] for x in sorted(source_first.items(), key=lambda x: x[1])]
    company_detail = [{"tk": tk, "n": name_by_ticker.get(tk, tk)} for tk in sorted(companies)]
    hot_rows.append({
        "w": k, "score": score, "count": count, "companies": len(companies),
        "company_detail": company_detail,
        "capital": sum(1 for ev in events if ev.get("type") in CAPITAL_TYPES or ev.get("amt")),
        "first_seen": first.get("date", ""), "first_n": first.get("name", ""),
        "first_tk": first.get("ticker", ""), "first_url": first.get("url", ""),
        "source_path": source_path, "evidence_events": len(events),
        "weekly": [v["weekly"].get(wk, 0) for wk in week_dates]
    })
feed["hotwords"] = sorted(hot_rows, key=lambda x: (-x["score"], -x["count"], x["w"]))[:18]
signal_dates = sorted(os.path.basename(p)[:10] for p in glob.glob(os.path.join(BASE, "signals", "????-??-??.jsonl")))
feed["hotword_window"] = {
    "requested_days": HOT_DAYS,
    "available_from": signal_dates[0] if signal_dates else "",
    "through": TODAY.isoformat(),
    "formula": "频次×跨公司数",
    "weeks": week_dates
}

# 会议原声(meeting-collector 周采 + earnings_call 公开媒体索引)
try:
    mfiles = sorted(glob.glob(os.path.join(BASE, "meetings", "*.md")))
    if mfiles:
        mf = mfiles[-1]
        feed["meetings"] = {"date": os.path.basename(mf)[:-3],
                            "md": open(mf, encoding="utf-8").read()[:12000]}
except Exception:
    pass

try:
    media_rows = []
    media_seen = set()
    cutoff = (TODAY - datetime.timedelta(days=180)).isoformat()
    for path in glob.glob(os.path.join(BASE, "by_company", "*.jsonl")):
        for row in load_jsonl(path):
            if row.get("type") != "earnings_call" or row.get("date", "") < cutoff:
                continue
            key = (row.get("ticker", ""), row.get("date", ""), row.get("media_url", ""))
            if key in media_seen:
                continue
            media_seen.add(key)
            media_rows.append({k: row.get(k, "") for k in
                               ("ticker", "name", "market", "date", "title", "media_url", "transcript_url", "source")})
    media_rows.sort(key=lambda x: (x["date"], x["ticker"]), reverse=True)
    if media_rows:
        feed.setdefault("meetings", {"date": TODAY.isoformat(), "md": ""})["media"] = media_rows[:20]
except Exception:
    pass

# 全公司名录(供搜索——7天没披露的公司也能搜到进档案)
try:
    wl = json.load(open(os.path.join(SCRIPTS, "watchlist-disclosure.json"), encoding="utf-8"))
    feed["cos"] = [{"n": w.get("name", ""), "tk": w["ticker"], "m": w["market"]} for w in wl]
except Exception:
    feed["cos"] = []

# 日历：公告侧仍取未来21天；财报页覆盖整个关注池。
# 第一优先级是 calendar 中的官宣/人工窗口；其余公司用历史披露节奏推算下一窗口，
# 必须显式标记为 estimate，绝不把算法日期画成已官宣。
try:
    cal = json.load(open(os.path.join(BASE, "calendar.json"), encoding="utf-8"))
    end = (TODAY + datetime.timedelta(days=21)).isoformat()
    feed["calendar"] = [e for e in cal if TODAY.isoformat() <= e["date"] <= end]
    # 页面只展示未来三个月；更远日期继续由历史库保留，不进入首屏长列表。
    earnings_end_date = TODAY + datetime.timedelta(days=92)
    earnings_end = earnings_end_date.isoformat()
    fin_words = ("财报", "决算", "业绩", "中报", "季报", "年报", "Q1", "Q2", "Q3", "Q4", "FY")
    by_ticker = {str(x.get("ticker", "")): x for x in wl}
    by_name = {str(x.get("name", "")): x for x in wl}
    earnings, scheduled_tickers = [], set()
    for event in cal:
        if not (TODAY.isoformat() <= event.get("date", "") <= earnings_end):
            continue
        if not any(word in event.get("event", "") for word in fin_words):
            continue
        if any(word in event.get("event", "") for word in ("解禁", "投产", "交割")):
            continue
        meta = by_ticker.get(str(event.get("ticker", ""))) or by_name.get(str(event.get("name", ""))) or {}
        text = event.get("event", "")
        status = "calendar_estimate" if any(word in text for word in ("待确认", "待官宣", "估计", "候选", "窗口", "复核")) else "confirmed"
        inferred_market = meta.get("market", "")
        if not inferred_market and str(event.get("ticker", "")).endswith(".T"):
            inferred_market = "JP"
        row = dict(event)
        canonical_ticker = meta.get("ticker") or event.get("ticker") or ""
        row.update({"ticker": canonical_ticker,
                    "market": inferred_market, "status": status,
                    "date_label": "关注日" if status != "confirmed" else "披露日",
                    "basis": "calendar登记"})
        earnings.append(row)
        if canonical_ticker:
            scheduled_tickers.add(str(canonical_ticker))

    # 对 calendar 未覆盖的关注公司，根据近三年官方财报发布日期间隔推算下一窗口。
    # 这是排期层，不是业绩判断；页面会标为“历史推算”。
    fallback_days = {"US": 91, "CN": 92, "HK": 183}
    for meta in wl:
        ticker = str(meta.get("ticker", ""))
        if not ticker or ticker in scheduled_tickers:
            continue
        parsed = []
        for value in sorted(_fin_dates.get(ticker, set())):
            try:
                parsed.append(datetime.date.fromisoformat(value))
            except Exception:
                pass
        if not parsed:
            continue
        gaps = [(parsed[i] - parsed[i - 1]).days for i in range(1, len(parsed))]
        usable = [g for g in gaps[-8:] if 40 <= g <= 400]
        cadence = int(round(statistics.median(usable))) if usable else fallback_days.get(meta.get("market"), 92)
        cadence = max(45, min(cadence, 400))
        next_date = parsed[-1] + datetime.timedelta(days=cadence)
        while next_date < TODAY:
            next_date += datetime.timedelta(days=cadence)
        if next_date > earnings_end_date:
            continue
        sample = min(len(usable) + 1, len(parsed))
        earnings.append({
            "date": next_date.isoformat(), "ticker": ticker,
            "name": meta.get("name") or ticker, "market": meta.get("market", ""),
            "status": "history_estimate", "date_label": "推算窗口",
            "basis": "近%d期官方财报发布日期" % sample,
            "event": "%s（按近%d期披露节奏约%d天推算·待公司官宣）" %
                     (next_date.isoformat(), sample, cadence),
        })
    feed["earnings_calendar"] = sorted(earnings, key=lambda x: (x["date"], x.get("name", "")))
    watch_tickers = {str(x.get("ticker", "")) for x in wl if x.get("ticker")}
    event_tickers = {str(x.get("ticker", "")) for x in earnings if x.get("ticker")}
    feed["earnings_calendar_window"] = {"from": TODAY.isoformat(), "through": earnings_end,
                                         "confirmed": sum(x["status"] == "confirmed" for x in earnings),
                                         "calendar_estimate": sum(x["status"] == "calendar_estimate" for x in earnings),
                                         "history_estimate": sum(x["status"] == "history_estimate" for x in earnings),
                                         "covered": len(event_tickers & watch_tickers),
                                         "watchlist": len(watch_tickers),
                                         "extra_markets": len(event_tickers - watch_tickers),
                                         "schedule": "每日 07:00 / 19:30（UTC+8）"}
except Exception:
    feed["calendar"] = []
    feed["earnings_calendar"] = []

os.makedirs("/tmp/s1out", exist_ok=True)
tmp = "/tmp/s1out/feed.json"
json.dump(feed, open(tmp, "w", encoding="utf-8"), ensure_ascii=False)
os.system("sudo mkdir -p %s && sudo cp %s %s/feed.json" % (OUT_DIR, tmp, OUT_DIR))
print("[S1流] v3 发布: 全量%d条 财报公司%d 玲姐尺%d词 前哨尺%d词 涌现雷达%d词" % (
    len(feed["all"]), len(feed["fin_by_co"]), len(feed["trends"]["玲姐尺"]), len(feed["trends"]["前哨尺"]), len(feed["hotwords"])))
