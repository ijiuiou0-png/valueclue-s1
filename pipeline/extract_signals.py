#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
extract_signals.py — 涌现层·信号抽取器（框架正本第八章）
输入: disclosures/YYYY-MM-DD.jsonl（披露管道产出）
处理: 重大披露补官方原文摘录，再调 sub2 批处理——每条披露抽{13类信号+方向+金额}+产业关键词
输出: signals/YYYY-MM-DD.jsonl + hotwords/YYYY-WW.json(周累计+词源事件) + alerts/YYYY-MM-DD.jsonl
监控: 失败写 [涌现]FAIL 行(体检器捕获);告警由判官日报消化
用法: python3 extract_signals.py [--date YYYY-MM-DD]
环境: SUB2_KEY 从 ~/.signals.env 读(权限600,不落代码)
"""
import json, os, sys, datetime, urllib.request, argparse, collections, re
import html, subprocess
from html.parser import HTMLParser

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
# 模型分工(2026-07-13 CEO令): 执行/采集类默认 xAI Grok 4.5(不用4.3)·Claude 仅作 fallback
GROK = "http://${GATEWAY_IP}:8080/v1/chat/completions"  # openai协议
GROK_MODEL = "grok-4.5"
SUB2 = "http://${GATEWAY_IP}:8080/v1/messages"           # anthropic协议(fallback)
MODEL = "claude-sonnet-5"

TAXONOMY = ["扩产", "减产", "价格变动", "大额订单", "砍单", "技术突破",
            "客户变动", "融资稀释", "回购增持", "监管牌照", "用户与用量", "人事变动", "并购重组"]
# 硬资产类: 扩产/减产/价格/订单/砍单/技术突破(量产流片) | 软件模型类: 技术突破(发版)/融资稀释/用户与用量/监管牌照

MAX_DOC_BYTES = 12 * 1024 * 1024
MAX_DOC_CHARS = 2400
MAX_BATCH_DOC_CHARS = 12000
DOC_UA = {"User-Agent": "ValueClue Research ${SEC_CONTACT_EMAIL}"}

def _clean_text(text):
    text = html.unescape(text or "")
    text = re.sub(r"\s+", " ", text).strip()
    return text

def _html_text(data):
    text = data.decode("utf-8", "ignore")
    text = re.sub(r"(?is)<(script|style).*?>.*?</\1>", " ", text)
    text = re.sub(r"(?s)<[^>]+>", " ", text)
    return _clean_text(text)

class _FinancialTableParser(HTMLParser):
    """从 Inline XBRL 中保留可读财务表，避免前 9k 字只截到标签元数据。"""
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.depth = 0
        self.buf = []
        self.tables = []

    def handle_starttag(self, tag, attrs):
        tag = tag.lower()
        if tag == "table":
            self.depth += 1
        elif self.depth and tag in ("tr", "p", "div", "br"):
            self.buf.append("\n")
        elif self.depth and tag in ("td", "th"):
            self.buf.append(" | ")

    def handle_endtag(self, tag):
        if tag.lower() == "table" and self.depth:
            self.depth -= 1
            if self.depth == 0:
                text = " ".join(self.buf)
                text = re.sub(r"[ \t]+", " ", text)
                text = re.sub(r"\s*\n\s*", "\n", text).strip(" |\n")
                if text:
                    self.tables.append(text)
                self.buf = []

    def handle_data(self, data):
        if self.depth:
            self.buf.append(data)

def _html_financial_text(data, max_chars):
    """按财务信息密度挑选表格，并保留原表序号供证据定位。"""
    raw = data.decode("utf-8", "ignore")
    parser = _FinancialTableParser()
    try:
        parser.feed(raw)
    except Exception:
        return _html_text(data)[:max_chars]
    keywords = {
        "consolidated statements": 10, "balance sheets": 8, "cash flows": 8,
        "revenues": 5, "net income": 5, "operating income": 4,
        "gross profit": 4, "capital expenditures": 4, "segment": 2,
        "guidance": 2, "three months ended": 2, "six months ended": 2,
    }
    ranked = []
    for index, table in enumerate(parser.tables, 1):
        low = table.lower()
        score = sum(weight * low.count(term) for term, weight in keywords.items())
        score += min(len(re.findall(r"\d[\d,.]*", table)), 60) / 10.0
        if score >= 3:
            ranked.append((score, index, table))
    ranked.sort(reverse=True)
    chunks, used = [], 0
    for _, index, table in ranked:
        chunk = "[SEC XBRL table %d]\n%s" % (index, table)
        if used and used + len(chunk) > max_chars:
            continue
        chunks.append(chunk)
        used += len(chunk) + 2
        if used >= max_chars:
            break
    return "\n\n".join(chunks)[:max_chars] if chunks else _html_text(data)[:max_chars]

def _pdf_text(data):
    try:
        proc = subprocess.run(["pdftotext", "-layout", "-", "-"], input=data,
                              stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
                              timeout=30, check=False)
        if proc.returncode == 0:
            return _clean_text(proc.stdout.decode("utf-8", "ignore"))
    except (OSError, subprocess.SubprocessError):
        pass
    return ""

def disclosure_excerpt(row, max_chars=MAX_DOC_CHARS, financial=False):
    """读取重大披露原文；失败时返回空串，让抽取器安全退回标题。"""
    if row.get("tier") != "重大":
        return ""
    data, content_type = b"", ""
    local = str(row.get("local", "") or "")
    if local:
        root = os.path.realpath(os.path.join(BASE, "disclosures"))
        path = os.path.realpath(os.path.join(root, local))
        if path.startswith(root + os.sep) and os.path.isfile(path):
            with open(path, "rb") as f:
                data = f.read(MAX_DOC_BYTES + 1)
    if not data and row.get("url"):
        try:
            req = urllib.request.Request(row["url"], headers=DOC_UA)
            with urllib.request.urlopen(req, timeout=30) as resp:
                content_type = (resp.headers.get("Content-Type") or "").lower()
                data = resp.read(MAX_DOC_BYTES + 1)
        except Exception:
            return ""
    if not data or len(data) > MAX_DOC_BYTES:
        return ""
    url = str(row.get("url", "")).lower()
    is_pdf = data.startswith(b"%PDF-") or "application/pdf" in content_type or url.endswith(".pdf")
    if is_pdf:
        text = _pdf_text(data)
    elif financial and "sec.gov" in url:
        text = _html_financial_text(data, max_chars)
    else:
        text = _html_text(data)
    return text[:max_chars]

def load_env(name):
    p = os.path.expanduser("~/.signals.env")
    try:
        for l in open(p):
            if l.startswith(name + "="): return l.strip().split("=", 1)[1]
    except Exception:
        pass
    return ""

def load_key():
    k = load_env("SUB2_KEY")
    if not k: raise SystemExit("[涌现]FAIL 无SUB2_KEY")
    return k

def _grok(prompt, retries=2):
    """主路: grok-4.5(openai协议·执行层默认)"""
    gk = load_env("SUB2_GROK_KEY")
    if not gk: return None
    body = json.dumps({"model": GROK_MODEL, "max_tokens": 1500,
                       "messages": [{"role": "user", "content": prompt}]})
    for att in range(retries + 1):
        try:
            req = urllib.request.Request(GROK, data=body.encode(),
                headers={"Authorization": "Bearer " + gk, "content-type": "application/json"})
            with urllib.request.urlopen(req, timeout=120) as r:
                d = json.loads(r.read())
            return d["choices"][0]["message"]["content"]
        except Exception:
            if att == retries: return None
    return None

def llm(key, prompt, retries=2):
    r = _grok(prompt, retries)
    if r is not None: return r
    # fallback: claude-sonnet-5(anthropic协议)——grok 全失败才走
    body = json.dumps({"model": MODEL, "max_tokens": 1500,
                       "messages": [{"role": "user", "content": prompt}]})
    for att in range(retries + 1):
        try:
            req = urllib.request.Request(SUB2, data=body.encode(),
                headers={"x-api-key": key, "anthropic-version": "2023-06-01",
                         "content-type": "application/json"})
            with urllib.request.urlopen(req, timeout=90) as r2:
                d = json.loads(r2.read())
            for blk in d.get("content", []):
                if blk.get("type") == "text":
                    return blk["text"]
            return ""
        except Exception:
            if att == retries: raise
    return ""

PROMPT = """你是投研信号抽取器。对下面这批公司披露材料,逐条判断是否含以下13类信号之一。若有正文摘录，以正文事实为准；没有正文时仅按标题判断:
%s
输出严格JSON数组,每个有信号的条目一个对象(无信号的跳过):
[{"i":条目序号,"type":"12类之一","dir":"利好|利空|中性","amt":"金额(带单位,无则空)","kw":["0-3个产业趋势词——只要【产业行为/技术方向】词(如'扩产''产能爬坡''良率''新品发布''量产''HBM4''玻璃基板''混合键合''封测')。禁止事件词(配售/回购/增发/可转债/定增/股权激励/董监事/股东大会/募投/诉讼——这些是公告分类不是趋势),禁止泛词('半导体''公司')"],"note":"一句话(≤25字)"}]
只输出JSON,无其他文字。披露列表:
%s"""

def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--date", default=datetime.date.today().isoformat())
    a = ap.parse_args()
    key = load_key()
    src = os.path.join(BASE, "disclosures", "%s.jsonl" % a.date)
    if not os.path.exists(src):
        print("[涌现]FAIL 无当日披露文件"); return
    rows = [json.loads(l) for l in open(src, encoding="utf-8")]
    if not rows:
        print("[涌现] 当日0条,跳过"); return

    os.makedirs(os.path.join(BASE, "signals"), exist_ok=True)
    os.makedirs(os.path.join(BASE, "hotwords"), exist_ok=True)
    os.makedirs(os.path.join(BASE, "alerts"), exist_ok=True)

    signals, kw_counter, kw_companies = [], collections.Counter(), collections.defaultdict(set)
    kw_events = collections.defaultdict(list)
    major_docs = sum(1 for r in rows if r.get("tier") == "重大")
    doc_loaded = 0
    BATCH = 25
    for b in range(0, len(rows), BATCH):
        chunk = rows[b:b+BATCH]
        listing_rows, doc_budget = [], MAX_BATCH_DOC_CHARS
        for i, r in enumerate(chunk):
            line = "%d. [%s]%s(%s): %s" % (i, r["market"], r["name"] or r["ticker"], r["tier"], r["title"][:80])
            excerpt = disclosure_excerpt(r) if doc_budget > 0 else ""
            if excerpt:
                doc_loaded += 1
                excerpt = excerpt[:doc_budget]
                doc_budget -= len(excerpt)
                line += "\n正文摘录: " + excerpt
            listing_rows.append(line)
        listing = "\n".join(listing_rows)
        items = []
        for attempt in range(2):
            try:
                out = llm(key, PROMPT % ("/".join(TAXONOMY), listing))
                m = re.search(r"\[.*\]", out, re.S)
                items = json.loads(m.group(0)) if m else []
                break
            except Exception as e:
                if attempt == 1:
                    print("[涌现]WARN batch%d 抽取失败(重试后): %s" % (b, e))
        for it in items:
            try:
                r = chunk[int(it["i"])]
            except Exception: continue
            KW_BLACK = {"配售","回购","增发","可转债","定增","股权激励","募投","董事","监事","股东大会","诉讼","H股","分红","利润分配","质押"}
            valid_kw = []
            for k in it.get("kw", []):
                k = str(k).strip()
                if 1 < len(k) <= 12 and not any(b in k for b in KW_BLACK):
                    if k in valid_kw:
                        continue
                    valid_kw.append(k)
                    kw_counter[k] += 1
                    kw_companies[k].add(r["ticker"])
                    form = str(r.get("form", "") or "公告")
                    source = "SEC" if form.upper() in {"10-K", "10-Q", "8-K", "6-K", "20-F"} else "公告"
                    kw_events[k].append({
                        "date": a.date, "ticker": r["ticker"], "name": r.get("name") or r["ticker"],
                        "market": r["market"], "source": source, "form": form,
                        "type": it.get("type", ""), "amt": it.get("amt", ""),
                        "title": r["title"][:120], "url": r.get("url", "")
                    })
            sig = {"date": a.date, "ticker": r["ticker"], "name": r["name"], "market": r["market"],
                   "type": it.get("type", ""), "dir": it.get("dir", ""), "amt": it.get("amt", ""),
                   "note": it.get("note", ""), "keywords": valid_kw,
                   "title": r["title"][:80], "url": r.get("url", "")}
            if sig["type"] in TAXONOMY:
                signals.append(sig)

    sf = os.path.join(BASE, "signals", "%s.jsonl" % a.date)
    with open(sf, "w", encoding="utf-8") as f:
        for s in signals: f.write(json.dumps(s, ensure_ascii=False) + "\n")

    # 热词周累计(跨公司共现权重: score=次数×不同公司数)
    week = datetime.date.fromisoformat(a.date).strftime("%G-W%V")
    hf = os.path.join(BASE, "hotwords", "%s.json" % week)
    hot = json.load(open(hf, encoding="utf-8")) if os.path.exists(hf) else {}
    for k, c in kw_counter.items():
        e = hot.setdefault(k, {"count": 0, "companies": []})
        # v3 起保留逐次词源事件，重跑同一天不会重复累计；旧周文件的历史计数保留为 legacy_count。
        if "events" not in e:
            e["legacy_count"] = int(e.get("count", 0))
            e["events"] = []
        seen = {"|".join([str(x.get("date", "")), str(x.get("ticker", "")),
                          str(x.get("url", "")), k]) for x in e["events"]}
        for ev in kw_events[k]:
            eid = "|".join([ev["date"], ev["ticker"], ev.get("url", ""), k])
            if eid not in seen:
                e["events"].append(ev)
                seen.add(eid)
        e["events"].sort(key=lambda x: (x.get("date", ""), x.get("ticker", ""), x.get("url", "")))
        e["count"] = int(e.get("legacy_count", 0)) + len(e["events"])
        event_companies = {x.get("ticker", "") for x in e["events"] if x.get("ticker")}
        e["companies"] = sorted(set(e.get("companies", [])) | kw_companies[k] | event_companies)
        if e["events"]:
            e["first_seen"] = e["events"][0].get("date", "")
    json.dump(hot, open(hf, "w", encoding="utf-8"), ensure_ascii=False, indent=1)

    # 告警: 高危信号类型 或 命中判断库标的
    ALERT_TYPES = {"扩产", "砍单", "融资稀释", "减产", "大额订单", "并购重组"}
    alerts = [s for s in signals if s["type"] in ALERT_TYPES]
    af = os.path.join(BASE, "alerts", "%s.jsonl" % a.date)
    with open(af, "w", encoding="utf-8") as f:
        for s in alerts: f.write(json.dumps(s, ensure_ascii=False) + "\n")

    top_hot = sorted(hot.items(), key=lambda x: -x[1]["count"] * len(x[1]["companies"]))[:8]
    print("[原文] 重大正文=%d/%d" % (doc_loaded, major_docs))
    print("[涌现] %s 信号=%d 告警=%d 热词周累计=%d" % (a.date, len(signals), len(alerts), len(hot)))
    for s in alerts[:6]:
        print("[告警] %s %s %s(%s): %s %s" % (s["type"], s["dir"], s["name"], s["ticker"], s["amt"], s["note"]))
    print("[热词] " + " | ".join("%s(%d次/%d家)" % (k, v["count"], len(v["companies"])) for k, v in top_hot))

if __name__ == "__main__":
    main()
