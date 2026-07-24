#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""fetch_yt_transcripts.py — CEO 公开讲话/大会视频 → 逐字稿采集(M1 版)

架构(Owner 2026-07-23 定): 重活放 M1,Server B 只收产物。本脚本跑在 M1 系统 crontab
(不走 openclaw cron —— gateway 会僵死,系统 cron 不受影响)。

主路径: 官方频道白名单 → yt-dlp 列近期视频(不下载) → youtube-transcript-api 抓字幕文本
兜底:   无字幕视频只记 pending 清单,whisper 转写另跑(重活分离,先验主路径)
产物:   ~/meeting-collector-output/yt/<日期>-<视频ID>.md(含来源/频道/时间戳链接,S级一手标注)
状态:   ~/meeting-collector-output/yt/state.json 已见视频ID,幂等
纪律:   官方频道=S级一手;英文逐字稿原样存档不翻译(研判产出才是中文);凭据不打印。
用法:   python3 fetch_yt_transcripts.py [--days 7] [--per-channel 5]
"""
import json, os, re, subprocess, sys, datetime

HOME = os.path.expanduser("~")
OUT = os.path.join(HOME, "meeting-collector-output", "yt")
STATE = os.path.join(OUT, "state.json")
os.makedirs(OUT, exist_ok=True)

# 白名单:先 6 个频道试跑一周(2026-07-23 脑定),稳定后再扩
# handle 由部署时逐一实测校正(先试再下结论,别信记忆里的 handle)
CHANNELS = [
    ("NVIDIA",        "https://www.youtube.com/@NVIDIA/videos"),
    ("TSMC",          "https://www.youtube.com/channel/UC02yNxGj2MxhynehcWSxcLg/videos"),
    ("AMD",           "https://www.youtube.com/@amd/videos"),
    ("Microsoft",     "https://www.youtube.com/@Microsoft/videos"),
    ("BloombergTV",   "https://www.youtube.com/@markets/videos"),
    ("BG2Pod",        "https://www.youtube.com/@bg2pod/videos"),
]

DAYS = 7
PER = 5
args = sys.argv[1:]
for i, a in enumerate(args):
    if a == "--days" and i + 1 < len(args):
        DAYS = int(args[i + 1])
    if a == "--per-channel" and i + 1 < len(args):
        PER = int(args[i + 1])

def sh(cmd, timeout=120):
    return subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)

state = {"seen": []}
if os.path.exists(STATE):
    try:
        state = json.load(open(STATE, encoding="utf-8"))
    except Exception:
        pass
seen = set(state.get("seen", []))

cutoff = (datetime.date.today() - datetime.timedelta(days=DAYS)).strftime("%Y%m%d")
new_docs, pending, errors = [], [], []

for ch_name, ch_url in CHANNELS:
    # 列最近视频:不下载,只拿 id/标题/日期/时长
    r = sh(["yt-dlp", "--flat-playlist", "--print",
            "%(id)s\t%(title)s\t%(upload_date,release_date|)s\t%(duration|0)s",
            "-I", "1:%d" % PER, ch_url], timeout=180)
    if r.returncode != 0:
        errors.append("%s: 列表失败 %s" % (ch_name, r.stderr.strip()[-120:]))
        continue
    for line in r.stdout.strip().split("\n"):
        parts = line.split("\t")
        if len(parts) < 2:
            continue
        vid, title = parts[0], parts[1]
        udate = parts[2] if len(parts) > 2 else ""
        dur = int(float(parts[3])) if len(parts) > 3 and parts[3] not in ("", "NA") else 0
        if vid in seen:
            continue
        if udate and udate != "NA" and udate < cutoff:
            continue
        if dur and dur < 300:      # 短于5分钟的宣传片/预告跳过
            seen.add(vid)
            continue
        # 抓字幕(youtube-transcript-api CLI 模块调用)
        cap = sh([sys.executable, "-m", "youtube_transcript_api", vid,
                  "--languages", "en", "zh-Hans", "zh", "--format", "text"], timeout=120)
        if cap.returncode != 0 or len(cap.stdout.strip()) < 500:
            pending.append({"vid": vid, "ch": ch_name, "t": title, "d": udate,
                            "why": "无字幕或过短,待 whisper"})
            seen.add(vid)
            continue
        dstr = ("%s-%s-%s" % (udate[:4], udate[4:6], udate[6:8])) if len(udate) == 8 else datetime.date.today().isoformat()
        fn = os.path.join(OUT, "%s-%s.md" % (dstr, vid))
        body = (
            "# %s\n\n- 频道: %s(官方,S级一手)\n- 日期: %s · 时长: %d 分钟\n"
            "- 链接: https://www.youtube.com/watch?v=%s\n- 采集: %s · 字幕原文未翻译,引用需回视频时间戳\n\n---\n\n%s\n"
            % (title, ch_name, dstr, dur // 60, vid,
               datetime.datetime.now().strftime("%Y-%m-%d %H:%M"), cap.stdout.strip())
        )
        open(fn, "w", encoding="utf-8").write(body)
        new_docs.append("%s | %s | %s" % (ch_name, dstr, title[:60]))
        seen.add(vid)

state["seen"] = sorted(seen)[-3000:]
if pending:
    pf = os.path.join(OUT, "pending_whisper.jsonl")
    with open(pf, "a", encoding="utf-8") as f:
        for p in pending:
            f.write(json.dumps(p, ensure_ascii=False) + "\n")
json.dump(state, open(STATE, "w", encoding="utf-8"), ensure_ascii=False)

print("[yt] 新逐字稿 %d | 待whisper %d | 频道错误 %d" % (len(new_docs), len(pending), len(errors)))
for d in new_docs:
    print("  +", d)
for e in errors:
    print("  !", e)
