#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""gen_report_queue.py — 底稿联动·重点池标的财报深读排队(Owner 2026-07-23 拍板)

机制: 重点池标的(config/watch_targets.json,L1-L3 ≥3★ + 全部4★,共25家)出定期报告(tier=财报)
  → 追加 report_queue.jsonl(幂等) → TG 通知 Owner+脑
  → 脑跑深度工作流出完整投研报告
  → 按 schemas/research-facts.schema.json 回写本篇已核验产业事实
  → desk 人审 → 上站+更新底稿
本脚本只管「排队+通知」,不烧深研 token。
用法: python3 gen_report_queue.py [--days 1] [--dry]
"""
import json, os, sys, glob, datetime, urllib.request, urllib.parse

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
CONF = os.path.join(BASE, "config", "watch_targets.json")
if not os.path.exists(CONF):
    CONF = os.path.join(BASE, "config-watch-targets.json")
QUEUE = os.path.join(BASE, "report_queue.jsonl")

DAYS, DRY = 1, False
a = sys.argv[1:]
for i, x in enumerate(a):
    if x == "--days" and i + 1 < len(a):
        DAYS = int(a[i + 1])
    if x == "--dry":
        DRY = True

def norm(t):
    """归一多市场代码: 0522.HK->00522 / 688120->688120 / NVDA->NVDA"""
    t = (t or "").strip().upper()
    if t.endswith(".HK"):
        return t[:-3].zfill(5)
    if t.endswith(".T"):
        return t
    return t

targets = {}
for x in json.load(open(CONF, encoding="utf-8")):
    targets[norm(x["ticker"])] = x

def load_jsonl(p):
    try:
        return [json.loads(l) for l in open(p, encoding="utf-8") if l.strip()]
    except Exception:
        return []

have = {r.get("k") for r in load_jsonl(QUEUE)}
today = datetime.date.today()
hits = []
for i in range(DAYS):
    d = (today - datetime.timedelta(days=i)).isoformat()
    for r in load_jsonl(os.path.join(BASE, "disclosures", d + ".jsonl")):
        tier = r.get("tier")
        if tier == "重大":
            # 重点池标的重大公告→TG即时分享(2026-07-24 Owner要求),不排深读只通知
            tk2 = norm(r.get("ticker", ""))
            if tk2 in targets:
                k2 = "MAJ|%s|%s|%s" % (r.get("date", d), tk2, (r.get("title") or "")[:40])
                if k2 not in have:
                    tgt2 = targets[tk2]
                    hits.append({"k": k2, "d": r.get("date", d), "tk": tk2, "n": tgt2["company"],
                                 "layer": tgt2["layer"], "stars": tgt2["stars"], "kind": "major",
                                 "form": (r.get("title") or "")[:80], "url": r.get("url", ""),
                                 "queued_at": datetime.datetime.now().strftime("%Y-%m-%d %H:%M"),
                                 "status": "notified"})
            continue
        if tier != "财报":
            continue
        tk = norm(r.get("ticker", ""))
        if tk not in targets:
            continue
        k = "%s|%s|%s" % (r.get("date", d), tk, (r.get("title") or r.get("form", ""))[:40])
        if k in have:
            continue
        tgt = targets[tk]
        hits.append({"k": k, "d": r.get("date", d), "tk": tk, "n": tgt["company"],
                     "layer": tgt["layer"], "stars": tgt["stars"],
                     "form": (r.get("title") or r.get("form", ""))[:80],
                     "url": r.get("url", ""), "queued_at": datetime.datetime.now().strftime("%Y-%m-%d %H:%M"),
                     "status": "queued", "facts_required": True,
                     "facts_schema": "schemas/research-facts.schema.json",
                     "facts_output": "research-facts/%s-%s.json" % (r.get("date", d), tk)})

if DRY:
    print("[report_queue] DRY 命中 %d:" % len(hits))
    for h in hits:
        print("  %s %s(%s) %d★ %s" % (h["d"], h["n"], h["tk"], h["stars"], h["form"]))
    raise SystemExit(0)

if hits:
    with open(QUEUE, "a", encoding="utf-8") as f:
        for h in hits:
            f.write(json.dumps(h, ensure_ascii=False) + "\n")
    # TG 通知(凭据从 api-keys.env 读,不打印)
    tok = cid = ""
    try:
        for line in open("${API_KEYS_ENV:-/etc/valueclue/api-keys.env}", encoding="utf-8"):
            if line.startswith("TELEGRAM_BOT_TOKEN="):
                tok = line.split("=", 1)[1].strip()
            elif line.startswith("TELEGRAM_CHAT_ID="):
                cid = line.split("=", 1)[1].strip()
    except Exception:
        pass
    if tok and cid:
        fin_h = [h for h in hits if h.get("kind") != "major"]
        maj_h = [h for h in hits if h.get("kind") == "major"]
        parts = []
        if fin_h:
            parts.append("📚 重点池财报落地,深读已排队:\n" + "\n".join(
                "· %s %s(%s) %d★ %s" % (h["d"], h["n"], h["tk"], h["stars"], h["form"][:40]) for h in fin_h[:8]) + "\n脑将出完整投研报告→desk 人审")
        if maj_h:
            parts.append("🔔 重点池标的重大公告:\n" + "\n".join(
                "· %s %s(%s) %s" % (h["d"], h["n"], h["tk"], h["form"][:46]) for h in maj_h[:8]) + "\n解读见 S1 公告页右栏")
        msg = "\n\n".join(parts)
        try:
            urllib.request.urlopen(
                "https://api.telegram.org/bot%s/sendMessage" % tok,
                data=urllib.parse.urlencode({"chat_id": cid, "text": msg}).encode(), timeout=20)
        except Exception as e:
            print("[report_queue] TG通知失败:", str(e)[:80])

total = len(load_jsonl(QUEUE))
print("[report_queue] 新排队 %d | 队列累计 %d" % (len(hits), total))
