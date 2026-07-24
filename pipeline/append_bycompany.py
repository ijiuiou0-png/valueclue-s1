#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""append_bycompany.py — 当日披露增量 append 进公司底稿档案(by_company/<ticker>.jsonl)
底稿=回填(近1年公告+近3年财报),本脚本负责每日保鲜; 跑在 server-b runner ⑦,append 后 rsync nas
"""
import json, os, datetime

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
BYCO = os.path.join(BASE, "by_company")
os.makedirs(BYCO, exist_ok=True)
TODAY = datetime.date.today().isoformat()

def main():
    src = os.path.join(BASE, "disclosures", "%s.jsonl" % TODAY)
    if not os.path.exists(src):
        print("[档案] 当日文件缺失"); return
    rows = [json.loads(l) for l in open(src, encoding="utf-8") if l.strip()]
    added = 0
    byt = {}
    for r in rows:
        byt.setdefault(r["ticker"], []).append(r)
    for t, items in byt.items():
        f = os.path.join(BYCO, "%s.jsonl" % t)
        seen = set()
        if os.path.exists(f):
            for l in open(f, encoding="utf-8"):
                try:
                    o = json.loads(l); seen.add((o.get("date"), o.get("title")))
                except Exception: pass
        with open(f, "a", encoding="utf-8") as fh:
            for r in items:
                if (r.get("date"), r.get("title")) in seen: continue
                fh.write(json.dumps(r, ensure_ascii=False) + "\n"); added += 1
    rc = os.system("rsync -aq --timeout=120 %s/ ${NAS_USER}@${NAS_HOST}:kb/disclosures/by_company/ 2>/dev/null" % BYCO)
    print("[档案] 新增%d条 涉及%d家 nas同步%s" % (added, len(byt), "OK" if rc == 0 else "FAIL"))

if __name__ == "__main__":
    main()
