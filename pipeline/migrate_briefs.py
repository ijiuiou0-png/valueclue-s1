#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""历史公告解读迁移到 schema 2:旧段落式(无 analysis 的 sig/big)逐条重生成,原地重写库。
用法: python3 migrate_briefs.py [--limit 80]  (幂等:已有 analysis 的跳过)"""
import json, os, sys, shutil, datetime, importlib.util

BASE = "${PIPELINE_HOME}"
STORE = os.path.join(BASE, "briefs", "briefs.jsonl")
LIMIT = 80
a = sys.argv[1:]
for i, x in enumerate(a):
    if x == "--limit" and i + 1 < len(a):
        LIMIT = int(a[i + 1])

# 复用 gen_briefs 的 ask/配置(以模块方式加载,避免复制认证与解析逻辑)
spec = importlib.util.spec_from_file_location("gb", os.path.join(BASE, "scripts", "gen_briefs.py"))
gb = importlib.util.module_from_spec(spec)
sys.argv = ["gen_briefs.py", "--days", "0", "--limit", "0"]   # 空跑参数,只要函数与常量
spec.loader.exec_module(gb)

rows = [json.loads(l) for l in open(STORE, encoding="utf-8") if l.strip()]
bak = STORE + ".bak." + datetime.datetime.now().strftime("%Y%m%d-%H%M")
shutil.copy(STORE, bak)
print("备份:", bak, "| 总行", len(rows))

done = fail = 0
for r in rows:
    if done >= LIMIT:
        break
    if r.get("analysis") or r.get("k", "").startswith("MAJ|"):
        continue
    if r.get("type") == "财报披露":
        continue
    item = {"date": r.get("d"), "ticker": r.get("tk"), "name": r.get("n"),
            "market": r.get("m"), "type": r.get("type"), "dir": r.get("dir"),
            "title": r.get("t"), "note": "", "amt": ""}
    try:
        out = gb.ask(item)
        if not isinstance(out, dict):
            raise ValueError("non-dict")
    except Exception as e:
        fail += 1
        print("skip", r.get("k", "")[:40], str(e)[:60])
        continue
    r["analysis_schema"] = 2
    r["analysis"] = out
    r["dir"] = out.get("direction", r.get("dir", ""))
    r["confidence"] = out.get("confidence", "低")
    r["brief"] = "\n".join([out.get("core_view", ""), out.get("core_conclusion", ""),
                            "；".join(out.get("impacts", [])), "；".join(out.get("risks", []))])
    r["migrated_at"] = datetime.datetime.now().strftime("%Y-%m-%d %H:%M")
    done += 1

tmp = STORE + ".tmp"
with open(tmp, "w", encoding="utf-8") as f:
    for r in rows:
        f.write(json.dumps(r, ensure_ascii=False) + "\n")
os.replace(tmp, STORE)
remain = sum(1 for r in rows if not r.get("analysis") and not r.get("k", "").startswith("MAJ|") and r.get("type") != "财报披露")
print("迁移完成 %d | 失败 %d | 剩余旧格式 %d" % (done, fail, remain))
