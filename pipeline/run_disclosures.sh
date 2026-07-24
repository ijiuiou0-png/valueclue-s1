#!/bin/bash
# 每日一手披露管道 runner v3 (server-b, 07:00 CST)
# v3 2026-07-12 CEO架构令: nas主抓(原文直落2TB数据盘)·server-b只做调度+检查+兜底+网站数据
cd ${PIPELINE_HOME}
LOG=logs/$(date +%F).log
TODAY=$(date +%F)
UMB=${NAS_USER}@${NAS_HOST}
mkdir -p logs disclosures

# ① 主抓: nas 直接抓三源(EDGAR/巨潮/HKEX 实测全通·家宽慢网限时50分钟)
echo "[主抓] nas 开始 $(date +%H:%M)" >> "$LOG"
# TZ 必带: nas 系统时区=UTC,07:00 CST 时它还是前一天,不带 TZ 会写错日期文件(2026-07-13 首跑教训)
ssh -o ConnectTimeout=20 $UMB "cd ~/disclosure-pipeline && TZ=Asia/Shanghai OVERSEA_PROXY=http://${SERVER_TS_IP}:8888 timeout 3000 python3 scripts/fetch_disclosures.py --days 1 >> logs/$TODAY.log 2>&1"
rsync -aq --timeout=60 $UMB:kb/disclosures/$TODAY.jsonl disclosures/ 2>/dev/null
N=$(wc -l < "disclosures/$TODAY.jsonl" 2>/dev/null || echo 0)
echo "[主抓] nas 完成 ${N}条 $(date +%H:%M)" >> "$LOG"

# ② 兜底: nas 掉线/缺数(家宽IP漂/断电) → server-b 本地补抓并推回
if [ "${N:-0}" -lt 5 ]; then
  echo "[兜底] nas缺数(${N}条) server-b补抓" >> "$LOG"
  python3 scripts/fetch_disclosures.py --days 1 >> "$LOG" 2>&1
  rsync -aq --timeout=120 disclosures/ $UMB:kb/disclosures/ 2>/dev/null
  # 云机不留存原文: 兜底下载的PDF推nas后即清
  rsync -aq --timeout=600 pdf/ $UMB:kb/disclosures/pdf/ 2>/dev/null && rm -rf pdf/*
fi

# ③ 体检: 条数统计+原文下载核对
python3 - >> "$LOG" 2>&1 <<'PY'
import json, datetime, collections
f = "disclosures/%s.jsonl" % datetime.date.today().isoformat()
try:
    rows = [json.loads(l) for l in open(f, encoding="utf-8")]
    tiers = dict(collections.Counter(r["tier"] for r in rows))
    majors = [r for r in rows if r["tier"] in ("财报", "重大")]
    nopdf = [r["ticker"] for r in majors if not r.get("local")]
    status = "OK" if not nopdf else "WARN-未下载:%s" % nopdf
    print("[体检] %s 条=%d 分级=%s 重大原文=%d/%d %s" % (
        datetime.date.today(), len(rows), tiers, len(majors)-len(nopdf), len(majors), status))
    for r in majors:
        print("[重大] %s %s %s: %s" % (r["market"], r["ticker"], r["name"], r["title"][:60]))
except FileNotFoundError:
    print("[体检] FAIL 当日文件缺失")
PY

# ④ 涌现层: 信号抽取+热词(框架第八章·sub2断供时跳过)
python3 scripts/extract_signals.py >> "$LOG" 2>&1 || echo '[涌现]FAIL(见日志)' >> "$LOG"
# ⑤ 时间节点日历检查(7天窗)
python3 scripts/gen_calendar.py --check >> "$LOG" 2>&1
# ⑥ 归档: server-b产物(信号/热词/告警)分目录推nas(修复v2混拍bug)
rsync -aq --timeout=60 signals/  $UMB:kb/disclosures/signals/  2>/dev/null \
  && rsync -aq --timeout=60 hotwords/ $UMB:kb/disclosures/hotwords/ 2>/dev/null \
  && rsync -aq --timeout=60 alerts/   $UMB:kb/disclosures/alerts/   2>/dev/null \
  && echo "[同步] nas OK" >> "$LOG" || echo "[同步] nas FAIL" >> "$LOG"
# ⑦ 公司档案增量: 当日条目按公司 append 进 by_company/(底稿库保鲜)
python3 scripts/append_bycompany.py >> "$LOG" 2>&1 || echo '[档案]FAIL' >> "$LOG"
# ⑦a2 底稿联动: 重点池标的财报落地→深读排队+TG通知(2026-07-23 Owner拍板)
python3 scripts/gen_report_queue.py >> "$LOG" 2>&1 || echo "[深读排队]FAIL" >> "$LOG"
# ⑦a 覆盖硬门禁: 防止 judgments/by_company 未加载时 0/0 假绿
if python3 scripts/check-disclosure-coverage.py --judgments-dir ${JUDGMENTS_HOME}/judgments --by-company-dir ${PIPELINE_HOME}/by_company --min-published 1 >> "$LOG" 2>&1; then
  echo "[覆盖体检]OK" >> "$LOG"
else
  echo "[覆盖体检]FAIL" >> "$LOG"
  TGTOK=$(grep ^TELEGRAM_BOT_TOKEN= ${API_KEYS_ENV:-/etc/valueclue/api-keys.env} | cut -d= -f2)
  TGCID=$(grep ^TELEGRAM_CHAT_ID= ${API_KEYS_ENV:-/etc/valueclue/api-keys.env} | cut -d= -f2)
  [ -n "$TGTOK" ] && curl -sm 15 "https://api.telegram.org/bot${TGTOK}/sendMessage" -d chat_id="${TGCID}" --data-urlencode text="⚠️ 覆盖体检 FAIL(judgments/by_company 加载异常或数量不齐),详见 hermes $LOG" >/dev/null
fi
# ⑦b ima 研报层增量: nas 只读拉取两知识库
ssh -o ConnectTimeout=20 $UMB "cd ~/ima-pipeline && timeout 1800 ~/emergence/venv/bin/python fetch_ima_research.py" >> "$LOG" 2>&1 || echo '[ima研报]FAIL' >> "$LOG"
# ⑧ S1情报流前端数据(先回拉 meeting-collector 会议周采产物)
rsync -aq --timeout=60 $UMB:kb/disclosures/meetings/ meetings/ 2>/dev/null
rsync -aq --timeout=60 $UMB:kb/disclosures/yt/ yt/ 2>/dev/null  # CEO视频逐字稿(M1采集经nas)
# ⑧a 公告解读生成(2026-07-23·逐条为什么·briefs永久累积,失败不阻断)
python3 scripts/gen_briefs.py >> "$LOG" 2>&1 || echo "[解读]FAIL(见日志)" >> "$LOG"
python3 scripts/gen_s1feed.py >> "$LOG" 2>&1
# ⑧a 财务事实层: 只有公司数非空、公司数对齐、门禁通过才发布
if python3 scripts/build_company_financials_v2.py \
  --watchlist scripts/watchlist-disclosure.json \
  --finfacts /var/www/html/ai-map/s1data/finfacts.json \
  --priority-config config/watch_targets.json \
  --out /tmp/s1out/company-financials-v2.json \
  --quality-out /tmp/s1out/financial-data-quality.json \
  --tasks-out /tmp/s1out/research-data-gaps.json >> "$LOG" 2>&1; then
  sudo install -m 0644 /tmp/s1out/company-financials-v2.json /var/www/html/ai-map/s1data/ \
    && sudo install -m 0644 /tmp/s1out/financial-data-quality.json /var/www/html/ai-map/s1data/ \
    && sudo install -m 0644 /tmp/s1out/research-data-gaps.json /var/www/html/ai-map/s1data/ \
    && echo "[财务事实层]OK" >> "$LOG" \
    || echo "[财务事实层]FAIL-发布" >> "$LOG"
else
  echo "[财务事实层]FAIL-门禁" >> "$LOG"
fi
# ⑨ 公司档案页面数据(co/<ticker>.json)
python3 scripts/gen_companyfeeds.py >> "$LOG" 2>&1
# ⑩ 首页初读(desk-public/daily-updates·另一agent 2026-07-14加,勿删)
python3 scripts/gen_daily_updates.py >> "$LOG" 2>&1 || echo '[首页初读]FAIL' >> "$LOG"
tail -8 "$LOG"
