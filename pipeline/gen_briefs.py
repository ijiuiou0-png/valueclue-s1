#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""gen_briefs.py — S1 解读栏·逐条公告深度解读生成器(2026-07-23 CEO 需求)

需求原话: 解读要写明"为什么"的理由和分析,可以长;每天新分析排最前,历史一直累积保留。

机制:
  输入  signals/*.jsonl(近 N 天) + disclosures/*.jsonl 中 tier=重大 且标题有实质内容者
  生成  每条一段 180-320 字分析(结论先行/传导机制/盯什么/信息边界),LLM 经 sub2 网关
  存储  briefs/briefs.jsonl 追加式永久累积,key=日期|代码|标题哈希,幂等不重算
  下游  gen_s1feed.py 读取 → feed["briefs"](新→旧) → S1 解读栏时间轴

纪律: 禁止编造公告没有的数字;调用失败跳过留给下一班;凭据不打印。
用法: python3 gen_briefs.py [--days 7] [--limit 40]
"""
import json, os, sys, glob, hashlib, datetime, time, urllib.request, re

from extract_signals import disclosure_excerpt

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
STORE_DIR = os.path.join(BASE, "briefs")
STORE = os.path.join(STORE_DIR, "briefs.jsonl")
os.makedirs(STORE_DIR, exist_ok=True)

API_BASE = ""
MODEL = ""
API_KEY = ""
try:
    for line in open("${API_KEYS_ENV:-/etc/valueclue/api-keys.env}", encoding="utf-8"):
        line = line.strip()
        if line.startswith("SUB2_BASE_URL="):
            API_BASE = line.split("=", 1)[1].strip().strip('"').strip("'")
        elif line.startswith("SUB2_CLAUDE_KEY="):
            API_KEY = line.split("=", 1)[1].strip().strip('"').strip("'")
        elif line.startswith("SUB2_CLAUDE_MODEL="):
            MODEL = line.split("=", 1)[1].strip().strip('"').strip("'")
except Exception:
    pass
if not (API_BASE and API_KEY and MODEL):
    print("[briefs] FATAL: api-keys.env 缺 SUB2_BASE_URL/SUB2_CLAUDE_KEY/SUB2_CLAUDE_MODEL")
    raise SystemExit(1)
API_BASE = API_BASE.rstrip("/")
if not API_BASE.endswith("/v1"):
    API_BASE += "/v1"

DAYS = 7
LIMIT = 40
args = sys.argv[1:]
FIN_ONLY = "--fin-only" in args
for i, a in enumerate(args):
    if a == "--days" and i + 1 < len(args):
        DAYS = int(args[i + 1])
    if a == "--limit" and i + 1 < len(args):
        LIMIT = int(args[i + 1])

def key_of(d, tk, title):
    return "%s|%s|%s" % (d, tk, hashlib.md5((title or "").encode()).hexdigest()[:10])

def load_jsonl(p):
    try:
        return [json.loads(l) for l in open(p, encoding="utf-8") if l.strip()]
    except Exception:
        return []

def load_json(p, default):
    try:
        return json.load(open(p, encoding="utf-8"))
    except Exception:
        return default

def official_source(item):
    url = str(item.get("url", "")).lower()
    if "sec.gov" in url: return "SEC EDGAR"
    if "hkexnews.hk" in url: return "HKEX 披露易"
    if "cninfo.com.cn" in url: return "巨潮资讯"
    return "发行人官方披露"

FINFACTS = {}
for _p in (os.path.join(BASE, "finfacts.json"), "/var/www/html/ai-map/s1data/finfacts.json"):
    FINFACTS = load_json(_p, {})
    if FINFACTS:
        break

def historical_baseline(ticker):
    row = FINFACTS.get(str(ticker), {})
    years = row.get("years", [])
    if not years:
        return {}
    idx = len(years) - 1
    def at(field):
        values = row.get(field, [])
        return values[idx] if idx < len(values) else None
    return {"period": years[idx], "display_unit": row.get("cur", ""),
            "revenue": at("rev"), "net_income_parent": at("np"),
            "gross_margin_pct": at("gm"), "operating_cash_flow": at("ocf"),
            "note": "仅作历史年度基线，不得冒充本次财报数字"}

# 已有解读(幂等)
have = set()
have_fin_v2 = set()
have_fin_source_v2 = set()
for r in load_jsonl(STORE):
    have.add(r.get("k"))
    if r.get("type") == "财报披露" and int(r.get("analysis_schema", 0)) >= 2:
        have_fin_v2.add((r.get("d", ""), r.get("tk", ""), r.get("t", "")))
        have_fin_source_v2.add(r.get("url") or "|".join((r.get("d", ""), r.get("tk", ""), r.get("t", ""))))

# 候选: 近 N 天信号 + 有实质标题的重大公告
today = datetime.date.today()
dates = [(today - datetime.timedelta(days=i)).isoformat() for i in range(DAYS)]
cands = []
seen_sig = set()
for d in dates:
    if not FIN_ONLY:
        for s in load_jsonl(os.path.join(BASE, "signals", d + ".jsonl")):
            k = key_of(s.get("date", d), s.get("ticker", ""), s.get("title", ""))
            seen_sig.add((s.get("ticker", ""), s.get("title", "")))
            if k not in have:
                cands.append(("sig", k, s))
    for r in load_jsonl(os.path.join(BASE, "disclosures", d + ".jsonl")):
        if r.get("tier") == "财报":
            title = r.get("title") or r.get("form", "")
            fin_key = (r.get("date", d), r.get("ticker", ""), title[:120])
            source_key = r.get("url") or "|".join(fin_key)
            if fin_key not in have_fin_v2 and source_key not in have_fin_source_v2:
                k = key_of(fin_key[0], fin_key[1], title) + "|v2"
                r = dict(r); r["_kind"] = "fin"
                cands.append(("fin", k, r))
                have_fin_source_v2.add(source_key)
            continue
        if r.get("tier") != "重大":
            continue
        if FIN_ONLY:
            continue
        title = r.get("title") or r.get("form") or ""
        if len(title) < 10:          # 裸 8-K/S-8 等无实质标题,分析只会是空话,跳过
            continue
        if (r.get("ticker", ""), title) in seen_sig:
            continue
        k = key_of(r.get("date", d), r.get("ticker", ""), title)
        if k not in have:
            cands.append(("big", k, r))

SYS = """你是 ValueClue 的公告解读员,读者是基金经理。只依据给出的公告要素(标题/附注/金额)与公开常识机制分析,禁止编造公告中没有的数字;公告没给的数值一律写"公告未披露"。
输出严格 JSON(不要任何 JSON 外的文字):
{"direction":"利好|利空|中性|待核验","confidence":"高|中|低","core_view":"一句话:方向+强弱+核心依据","core_conclusion":"一句话:对股东价值/我方判断意味着什么;信息不足就写尚不能判断","evidence":[{"metric":"公告要素(如发行规模/回购金额/转股价)","value":"公告给的数值或 公告未披露","comparison":"占股本/市值/成交量等量级对照;算不了写待核验","source_location":"公告标题|附注|金额字段"}],"impacts":["最多3条可能影响,写清传导路径(摊薄/现金流/产能/需求/壁垒/治理)"],"risks":["最多3条风险或证伪条件,含后续可验证节点(具体文件/数据/日期)"],"missing":["公告缺失的关键信息"]}
大白话,别堆术语;论据至少1条,量级对照能算就算。"""

SYS_FIN = """你是 ValueClue 的财报核验员。必须只使用提供的官方财报正文摘录与明确标为“历史年度基线”的数据，禁止补数字、禁止把历史基线冒充本期数字。资料不足时结论必须是待核验。

输出严格 JSON 对象，不要 markdown：
{"direction":"利好|利空|中性|待核验","confidence":"高|中|低","core_view":"一句话说明本季最重要的变化或数据缺口","core_conclusion":"一句话说明是否改变盈利趋势/资产负债/算力主线判断；资料不足就写尚不能判断","evidence":[{"metric":"具体科目或指标","value":"报告中的数值或未抽取","comparison":"同比/环比/指引对比；没有就写待核验","source_location":"官方来源+报告类型+摘录中的表/章节/字段；无法定位就写正文摘录未覆盖"}],"impacts":["最多3条可能影响，写清因果路径"],"risks":["最多3条风险或证伪条件"],"missing":["仍缺的关键数据"]}

硬要求：
1. evidence 至少2项，优先营收、归母净利、毛利率、经营现金流、资本开支、指引。
2. 每个结论必须能追溯到 evidence；摘录没有数据就明确“未抽取”，不可用行业常识填空。
3. core_view 与 core_conclusion 不得只是复述“公司发布了财报”。
4. impacts 要写“指标变化→业务/产业链→盈利或估值”的传导；risks 写可能使判断失效的条件。
5. 这是披露层核验，不自动改变 judgments 裁决。
6. direction 必须与 core_conclusion 一致；若收入增长但利润率/现金流明显恶化，应选中性或利空，不得仅因资本开支增加就标利好。"""

def ask(item):
    u = (
        "公告日期 %s\n公司 %s(%s.%s)\n信号类型 %s\n方向初判 %s\n标题: %s\n附注: %s\n金额: %s"
        % (item.get("date", ""), item.get("name") or item.get("ticker", ""),
           item.get("ticker", ""), item.get("market", ""),
           item.get("type") or ("重大事项" if item.get("tier") == "重大" else ""),
           item.get("dir", "待判"), item.get("title") or item.get("form", ""),
           item.get("note", ""), item.get("amt", "") or "未披露")
    )
    is_fin = item.get("_kind") == "fin"
    if is_fin:
        excerpt = disclosure_excerpt(dict(item, tier="重大"), max_chars=12000, financial=True)
        u += "\n官方来源: %s\n官方正文摘录:\n%s\n历史年度基线(不是本期数据): %s" % (
            official_source(item), excerpt or "未成功抽取正文",
            json.dumps(historical_baseline(item.get("ticker", "")), ensure_ascii=False))
    sys_prompt = SYS_FIN if is_fin else SYS
    payload = {
        "model": MODEL,
        "max_tokens": 2600 if is_fin else 600,
        "messages": [{"role": "system", "content": sys_prompt}, {"role": "user", "content": u}],
    }
    payload["response_format"] = {"type": "json_object"}
    if not is_fin:
        payload["max_tokens"] = 2000
    body = json.dumps(payload).encode()
    req = urllib.request.Request(
        API_BASE + "/chat/completions", data=body,
        headers={"Content-Type": "application/json", "Authorization": "Bearer " + API_KEY},
    )
    with urllib.request.urlopen(req, timeout=90) as resp:
        out = json.load(resp)
    text = out["choices"][0]["message"]["content"].strip()
    match = re.search(r"\{.*\}", text, re.S)
    if not match:
        raise ValueError("analysis is not JSON")
    analysis = json.loads(match.group(0))
    required = ("direction", "confidence", "core_view", "core_conclusion", "evidence", "impacts", "risks", "missing")
    if not isinstance(analysis, dict) or any(k not in analysis for k in required):
        raise ValueError("analysis missing required fields")
    min_ev = 2 if is_fin else 1
    if not isinstance(analysis["evidence"], list) or len(analysis["evidence"]) < min_ev:
        raise ValueError("analysis evidence is insufficient")
    return analysis

done = fail = 0
new_rows = []
for kind, k, it in cands:
    if done >= LIMIT:
        break
    try:
        output = ask(it)
        if not output or (isinstance(output, str) and len(output) < 40):
            raise ValueError("empty/short response")
    except Exception as e:
        fail += 1
        print("[briefs] skip %s: %s" % (k, str(e)[:80]))
        time.sleep(1)
        continue
    analysis = output if isinstance(output, dict) else None
    brief = output if isinstance(output, str) else "\n".join([
        output.get("core_view", ""), output.get("core_conclusion", ""),
        "；".join(output.get("impacts", [])), "；".join(output.get("risks", []))])
    row = {
        "k": k, "d": it.get("date", ""), "tk": it.get("ticker", ""),
        "n": it.get("name") or it.get("ticker", ""), "m": it.get("market", ""),
        "type": "财报披露" if kind == "fin" else (it.get("type") or ("重大事项" if kind == "big" else "")),
        "dir": it.get("dir", ""), "t": (it.get("title") or it.get("form", ""))[:120],
        "url": it.get("url", ""), "brief": brief,
        "gen_at": datetime.datetime.now().strftime("%Y-%m-%d %H:%M"), "model": MODEL,
    }
    if analysis:
        row.update({"analysis_schema": 2, "dir": analysis.get("direction", "待核验"),
                    "confidence": analysis.get("confidence", "低"), "analysis": analysis,
                    "source": {"provider": official_source(it), "form": it.get("form", ""),
                               "title": (it.get("title") or it.get("form", ""))[:120],
                               "url": it.get("url", "")}})
    new_rows.append(row)
    done += 1
    time.sleep(0.5)

if new_rows:
    with open(STORE, "a", encoding="utf-8") as f:
        for r in new_rows:
            f.write(json.dumps(r, ensure_ascii=False) + "\n")

total = len(load_jsonl(STORE))
print("[briefs] 候选%d 新增%d 失败%d 库存%d" % (len(cands), done, fail, total))
