#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""检查 published 标的一手底稿覆盖，并输出 desk 可消费的采集任务。只读 judgments/by_company。"""
import argparse
import datetime as dt
import glob
import json
import re
from pathlib import Path

PERIODIC_RE = re.compile(
    r"(?:财报|定期报告|年度报告|年报|半年度报告|半年报|季度报告|一季报|三季报|"
    r"annual report|quarterly report|\b10-[KQ](?:/A)?\b|\b20-F(?:/A)?\b)", re.I
)
RELATION_RE = re.compile(r"(?:投资者关系活动记录表|投资者关系活动|调研记录|investor relations)", re.I)
TEMPORARY_RE = re.compile(
    r"(?:临时公告|重大|公告|\b8-K\b|\b6-K\b|收购|回购|增发|配售|中标|诉讼|问询|处罚)", re.I
)
COMPANY_TICKER_FALLBACK = {"Amazon AWS": "AMZN"}


def load_json(path, default=None):
    try:
        return json.loads(Path(path).read_text(encoding="utf-8"))
    except Exception:
        return {} if default is None else default


def archive_key(company):
    ticker = str(company.get("ticker") or COMPANY_TICKER_FALLBACK.get(company.get("company"), "")).strip().upper()
    market = str(company.get("market") or "")
    if "港股" in market and not market.startswith("A股"):
        ticker = ticker.replace(".HK", "")
        return ticker.zfill(5) if ticker.isdigit() else ticker
    if "A股" in market:
        digits = re.sub(r"\D", "", ticker)
        return digits[:6] if len(digits) >= 6 else ""
    if "美股" in market:
        return ticker.split(".")[0]
    return ticker if re.fullmatch(r"[A-Z0-9.-]+", ticker) else ""


def load_published(judgments_dir):
    rows = []
    for path in sorted(Path(judgments_dir).glob("L*.json")):
        data = load_json(path)
        layer = data.get("layer") or path.stem
        for company in data.get("标的", []):
            if company.get("status") == "published":
                rows.append({**company, "layer": layer, "archive_key": archive_key(company)})
    return rows


def read_archive(path):
    rows = []
    try:
        for line in path.read_text(encoding="utf-8").splitlines():
            if line.strip():
                rows.append(json.loads(line))
    except Exception:
        return []
    return rows


def coverage_types(rows):
    found = {"periodic": False, "temporary": False, "investor_relation": False}
    for row in rows:
        text = " ".join(str(row.get(k) or "") for k in ("form", "tier", "title"))
        if PERIODIC_RE.search(text):
            found["periodic"] = True
        if RELATION_RE.search(text):
            found["investor_relation"] = True
        if TEMPORARY_RE.search(text) and not PERIODIC_RE.search(text) and not RELATION_RE.search(text):
            found["temporary"] = True
    return found


def inspect_target(target, by_company_dir, today, stale_days):
    key = target.get("archive_key", "")
    path = Path(by_company_dir) / f"{key}.jsonl" if key else None
    rows = read_archive(path) if path and path.exists() else []
    dates = [str(x.get("date") or "")[:10] for x in rows if re.fullmatch(r"\d{4}-\d{2}-\d{2}.*", str(x.get("date") or ""))]
    latest = max(dates) if dates else ""
    age_days = None
    if latest:
        try:
            age_days = (today - dt.date.fromisoformat(latest)).days
        except ValueError:
            pass
    types = coverage_types(rows)
    # “投资者关系活动记录表”是 A 股巨潮 relation 专栏类型；非 A 股不制造伪缺口。
    applicable = {
        "periodic": True,
        "temporary": True,
        "investor_relation": "A股" in str(target.get("market") or ""),
    }
    missing_types = [k for k, present in types.items() if applicable[k] and not present]
    exists = bool(path and path.exists())
    stale = age_days is None or age_days > stale_days
    gaps = []
    if not exists:
        gaps.append("底稿文件不存在")
    elif not rows:
        gaps.append("底稿文件为空或不可解析")
    if stale:
        gaps.append("最近90天无更新" if stale_days == 90 else f"最近{stale_days}天无更新")
    label = {"periodic": "定期报告", "temporary": "临时公告", "investor_relation": "投资者关系活动记录表"}
    gaps.extend("缺%s" % label[k] for k in missing_types)
    score = (100 if not exists else 0) + (40 if stale else 0) + 15 * len(missing_types)
    severe = (not exists) or stale or len(missing_types) >= 2
    return {
        "company": target.get("company", ""), "ticker": target.get("ticker", ""),
        "archive_key": key, "market": target.get("market", ""), "layers": target.get("layers", []),
        "path": str(path or ""), "exists": exists, "rows": len(rows), "latest": latest,
        "age_days": age_days, "types": types, "type_applicable": applicable,
        "missing_types": missing_types,
        "gaps": gaps, "score": score, "severe": severe,
    }


def collection_task(item):
    missing = "、".join(item["gaps"]) or "无"
    key = item.get("archive_key") or "unknown"
    type_label = {"periodic": "定期报告", "temporary": "临时公告", "investor_relation": "投资者关系活动记录表"}
    needed = "、".join(type_label[x] for x in item.get("missing_types", [])) or "最新一手披露"
    return {
        "id": f"coverage-{key}", "layer": "/".join(item.get("layers") or []),
        "target": f"{item['company']} {item.get('ticker','')}",
        "summary": f"{item['company']} 一手底稿覆盖缺口：{missing}",
        "source": item.get("path") or "published 标的无可用 ticker",
        "status": "collection", "format": "legacy", "confidence": "线索",
        "source_tier": "lead", "source_tier_label": "线索",
        "source_tier_reason": "一手底稿缺失、过期或类型不全",
        "collection_reason": "底稿覆盖缺口",
        "collection_instruction": f"需补抓{needed}的一手原文，并刷新 by_company",
    }


def build_report(judgments_dir, by_company_dir, stale_days=90):
    published = load_published(judgments_dir)
    grouped = {}
    for row in published:
        group_key = row.get("archive_key") or ("NO-TICKER:" + row.get("company", ""))
        if group_key not in grouped:
            grouped[group_key] = {**row, "layers": [row.get("layer", "")]}
        elif row.get("layer") not in grouped[group_key]["layers"]:
            grouped[group_key]["layers"].append(row.get("layer"))
    today = dt.date.today()
    items = [inspect_target(x, by_company_dir, today, stale_days) for x in grouped.values()]
    items.sort(key=lambda x: (-x["score"], x["company"]))
    tasks = [collection_task(x) for x in items if x["severe"]]
    return {
        "generated": dt.datetime.now().astimezone().isoformat(timespec="seconds"),
        "published_rows": len(published), "published_unique": len(grouped), "targets_checked": len(items),
        "threshold_days": stale_days,
        "summary": {
            "missing_file": sum(not x["exists"] for x in items),
            "stale": sum(x["age_days"] is None or x["age_days"] > stale_days for x in items),
            "missing_periodic": sum(not x["types"]["periodic"] for x in items),
            "missing_temporary": sum(not x["types"]["temporary"] for x in items),
            "missing_investor_relation": sum(
                x["type_applicable"]["investor_relation"] and not x["types"]["investor_relation"]
                for x in items
            ),
            "severe": len(tasks),
        },
        "items": items, "collection_tasks": tasks,
    }


def main():
    here = Path(__file__).resolve().parent
    ap = argparse.ArgumentParser()
    ap.add_argument("--judgments-dir", default=str(here.parent / "judgments"))
    ap.add_argument("--by-company-dir", default="${PIPELINE_HOME}/by_company")
    ap.add_argument("--json-out", default="${PIPELINE_HOME}/coverage-gaps.json")
    ap.add_argument("--stale-days", type=int, default=90)
    ap.add_argument("--top", type=int, default=10)
    ap.add_argument("--min-published", type=int, default=1,
                    help="低于该值视为输入未加载，直接非零退出，防止0/0假绿")
    ap.add_argument("--expected-published", type=int, default=0,
                    help="可选的 published 行数硬门禁；0表示不检查精确值")
    args = ap.parse_args()
    report = build_report(args.judgments_dir, args.by_company_dir, args.stale_days)
    out = Path(args.json_out)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    s = report["summary"]
    print("COVERAGE published=%d checked=%d missing_file=%d stale=%d severe=%d" % (
        report["published_rows"], report["targets_checked"], s["missing_file"], s["stale"], s["severe"]
    ))
    print("TOP %d GAPS" % args.top)
    for i, item in enumerate([x for x in report["items"] if x["gaps"]][:args.top], 1):
        print("%02d. %s | %s | latest=%s | %s" % (
            i, item["company"], item.get("archive_key") or "NO-TICKER",
            item.get("latest") or "NONE", "；".join(item["gaps"])
        ))
    print("coverage JSON -> %s | collection_tasks=%d" % (out, len(report["collection_tasks"])))
    gate_errors = []
    if report["published_rows"] < args.min_published:
        gate_errors.append("published_rows=%d < min_published=%d" % (report["published_rows"], args.min_published))
    if report["targets_checked"] == 0:
        gate_errors.append("targets_checked=0")
    if args.expected_published and report["published_rows"] != args.expected_published:
        gate_errors.append("published_rows=%d != expected_published=%d" % (
            report["published_rows"], args.expected_published))
    if gate_errors:
        print("COVERAGE_GATE FAIL | " + " | ".join(gate_errors))
        raise SystemExit(2)
    print("COVERAGE_GATE PASS")


if __name__ == "__main__":
    main()
