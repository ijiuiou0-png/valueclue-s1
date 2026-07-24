#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""公开业绩会议媒体采集器。

只抓公司 IR、SEC 附件和公开路演页；音视频只保存 URL，不下载媒体文件。
成功记录按幂等键追加到 disclosure-pipeline/by_company/<ticker>.jsonl；
miss 写入 agent-worker/logs/earnings-media/，只有音视频而无文字稿时进入 ASR 待办。

运行位置: server-b ~/agent-worker/scripts/
示例: python3 fetch_earnings_media.py --tickers NVDA 300308 00522
"""
from __future__ import annotations

import argparse
import datetime as dt
import glob
import json
import os
import re
import subprocess
import sys
import time
from pathlib import Path
from typing import Any
from urllib.parse import urljoin, urlparse
from urllib.robotparser import RobotFileParser

import requests

UA = "your-domain-earnings-media/1.0 (+https://your-domain.example/ai-map/)"
DEFAULT_DELAY = 2.0
MAX_TRANSCRIPT_BYTES = 5 * 1024 * 1024
MEDIA_LINK_ONLY_HOSTS = {"edge.media-server.com"}
LINK_ONLY_MEDIA_KINDS = {
    "official_ir_event_page",
    "official_ir_webcast_page",
    "official_webcast_link_on_ir_page",
}

# IR 页面结构各异。profile 只放已核验的一手入口；抓不到就记 miss，不猜链接。
# NVDA 当前一季用于首轮验收，后续季度只需字段级更新此 profile。
US_IR_PROFILES: dict[str, dict[str, str]] = {
    "NVDA": {
        "title": "NVIDIA 1st Quarter FY27 Financial Results",
        "date": "2026-05-20",
        "ir_url": "https://investor.nvidia.com/events-and-presentations/events-and-presentations/event-details/2026/NVIDIA-1st-Quarter-FY27-Financial-Results/default.aspx",
        "media_url": "https://events.q4inc.com/attendee/345403167",
        "transcript_url": "https://s201.q4cdn.com/141608511/files/doc_financials/2027/q1/NVDA-Q1-2027-Earnings-Call-20-May-2026-5_00-PM-ET.pdf",
    },
    "MSFT": {
        "title": "Microsoft Fiscal Year 2026 Third Quarter Earnings Conference Call",
        "date": "2026-04-29",
        "ir_url": "https://www.microsoft.com/en-us/investor/events/fy-2026/earnings-fy-2026-q3",
        "media_url": "https://www.microsoft.com/en-us/investor/events/fy-2026/earnings-fy-2026-q3",
        "media_kind": "official_ir_event_page",
        "transcript_url": "https://www.microsoft.com/en-us/investor/events/fy-2026/earnings-fy-2026-q3",
    },
    "GOOGL": {
        "title": "Alphabet 2026 Q1 Earnings Call",
        "date": "2026-04-29",
        "ir_url": "https://abc.xyz/investor/events/event-details/2026/2026-Q1-Earnings-Call-2026-nW8kCrBAKS/default.aspx",
        "media_url": "https://abc.xyz/investor/events/event-details/2026/2026-Q1-Earnings-Call-2026-nW8kCrBAKS/default.aspx",
        "media_kind": "official_webcast_link_on_ir_page",
        "transcript_url": "https://s206.q4cdn.com/479360582/files/doc_events/2026/Apr/29/Alphabet-2026_Q1_Earnings_Transcript.pdf",
    },
    "AMZN": {
        "title": "Q1 2026 Amazon.com, Inc. Earnings Conference Call",
        "date": "2026-04-29",
        "ir_url": "https://ir.aboutamazon.com/events/event-details/2026/Q1-2026-Amazoncom-Inc-Earnings-Conference-Call-/default.aspx",
        "media_url": "https://ir.aboutamazon.com/events/event-details/2026/Q1-2026-Amazoncom-Inc-Earnings-Conference-Call-/default.aspx",
        "media_kind": "official_ir_webcast_page",
        "transcript_url": "",
    },
    "META": {
        "title": "Meta Q1 2026 Earnings Call",
        "date": "2026-04-29",
        "ir_url": "https://investor.atmeta.com/investor-events/event-details/2026/Q1-2026-Earnings-Call/default.aspx",
        "media_url": "https://events.q4inc.com/attendee/723806435",
        "transcript_url": "https://s21.q4cdn.com/399680738/files/doc_financials/2026/q1/META-Q1-2026-Earnings-Call-Transcript.pdf",
    },
    "AMD": {
        "title": "AMD Fiscal First Quarter 2026 Financial Results",
        "date": "2026-05-05",
        "ir_url": "https://ir.amd.com/news-events/ir-calendar/detail/20260505-amd-fiscal-first-quarter-2026-financial-results",
        "media_url": "https://edge.media-server.com/mmc/p/ib3y8hjv",
        "transcript_url": "https://d1io3yog0oux5.cloudfront.net/_cece5bf914638d0ab16f558d26342d35/amd/db/841/9232/webcast_transcript/AMD_1Q_2026_Earnings.pdf",
    },
    "AVGO": {
        "title": "Q2 2026 Broadcom Earnings Conference Call",
        "date": "2026-06-03",
        "ir_url": "https://investors.broadcom.com/events/event-details/q2-2026-broadcom-earnings-conference-call",
        "media_url": "https://edge.media-server.com/mmc/p/xxmn2vvv",
        "transcript_url": "",
    },
}

HK_IR_PROFILES: dict[str, dict[str, str]] = {
    "00522": {
        "title": "ASMPT Q1 2026 Investor Conference Call",
        "date": "2026-04-22",
        "ir_url": "https://www.asmpt.com/en/investor-relations/financial-information/",
        "media_url": "https://www.asmpt.com/site/assets/files/85245/asmpt_q1_fy2026_investor_conference_call_audio.mp3",
        "media_kind": "audio_mp3",
        "transcript_url": "",
        "presentation_url": "https://www.asmpt.com/site/assets/files/85233/asmpt_q1_2026_investor_presentation.pdf",
    },
}


class PoliteSession:
    def __init__(self, delay: float = DEFAULT_DELAY, timeout: float = 25.0):
        self.delay = delay
        self.timeout = timeout
        self.session = requests.Session()
        self.session.headers.update({"User-Agent": UA, "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8"})
        self.last: dict[str, float] = {}
        self.robots: dict[str, RobotFileParser | None] = {}

    def _robot(self, url: str) -> RobotFileParser | None:
        p = urlparse(url)
        origin = f"{p.scheme}://{p.netloc}"
        if origin in self.robots:
            return self.robots[origin]
        rp = RobotFileParser()
        robots_url = origin + "/robots.txt"
        try:
            res = self.session.get(robots_url, timeout=self.timeout)
            if res.ok and "text" in res.headers.get("content-type", "text/plain"):
                rp.set_url(robots_url)
                rp.parse(res.text.splitlines())
                self.robots[origin] = rp
                return rp
        except requests.RequestException:
            pass
        self.robots[origin] = None
        return None

    def request(self, method: str, url: str, **kwargs: Any) -> requests.Response:
        rp = self._robot(url)
        if rp is not None and not rp.can_fetch(UA, url):
            raise PermissionError(f"robots_disallow:{url}")
        host = urlparse(url).netloc
        crawl_delay = rp.crawl_delay(UA) if rp else None
        if crawl_delay is None and rp:
            crawl_delay = rp.crawl_delay("*")
        wait = max(self.delay, float(crawl_delay or 0))
        elapsed = time.monotonic() - self.last.get(host, 0.0)
        if elapsed < wait:
            time.sleep(wait - elapsed)
        try:
            res = self.session.request(method, url, timeout=self.timeout, allow_redirects=True, **kwargs)
        finally:
            self.last[host] = time.monotonic()
        res.raise_for_status()
        return res


def clean_html(raw: str) -> str:
    raw = re.sub(r"(?is)<(script|style|noscript).*?>.*?</\1>", " ", raw)
    raw = re.sub(r"(?i)<br\s*/?>|</p>|</div>|</li>|</tr>", "\n", raw)
    raw = re.sub(r"(?s)<[^>]+>", " ", raw)
    raw = raw.replace("&nbsp;", " ").replace("&amp;", "&")
    raw = re.sub(r"[ \t\r\f\v]+", " ", raw)
    raw = re.sub(r"\n\s*\n+", "\n", raw)
    return raw.strip()


def extract_transcript(res: requests.Response) -> str:
    if len(res.content) > MAX_TRANSCRIPT_BYTES:
        raise ValueError("transcript_too_large")
    ctype = res.headers.get("content-type", "").lower()
    if "pdf" in ctype or res.content[:4] == b"%PDF":
        proc = subprocess.run(
            ["pdftotext", "-layout", "-", "-"], input=res.content,
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, check=False,
        )
        if proc.returncode != 0:
            raise RuntimeError("pdftotext_failed")
        return proc.stdout.decode("utf-8", "replace").strip()
    return clean_html(res.text)


def norm_market(value: str) -> str:
    value = value.upper()
    if value == "US" or "美股" in value: return "US"
    if value == "CN" or "A股" in value or "北交所" in value: return "CN"
    if value == "HK" or "港股" in value or value.endswith(".HK"): return "HK"
    return value


def norm_ticker(value: str, market: str) -> str:
    value = value.strip().upper()
    if market == "US":
        return value.removesuffix(".US")
    if market == "HK":
        stem = value.removesuffix(".HK")
        return stem.zfill(5) if stem.isdigit() else value
    return value


def load_watchlist(path: Path) -> list[dict[str, str]]:
    if not path.exists():
        return []
    rows = json.loads(path.read_text(encoding="utf-8"))
    out = []
    for x in rows:
        market = norm_market(x.get("market", ""))
        if x.get("ticker"):
            out.append({"ticker": norm_ticker(str(x["ticker"]), market), "name": x.get("name", ""), "market": market})
    return out


def load_published(judgments_dir: Path) -> list[dict[str, str]]:
    rows: list[dict[str, str]] = []
    for file in sorted(judgments_dir.glob("L*.json")):
        data = json.loads(file.read_text(encoding="utf-8"))
        for item in data.get("标的", []):
            if item.get("status") == "published" and item.get("ticker"):
                market = norm_market(item.get("market", ""))
                rows.append({"ticker": norm_ticker(str(item["ticker"]), market), "name": item.get("company", ""),
                             "market": market})
    return rows


def load_datajs_published(path: Path) -> list[dict[str, str]]:
    """读取生产 data.js 中由 judgments 发布生成的 published 镜像。"""
    if not path.exists():
        return []
    raw = path.read_text(encoding="utf-8", errors="replace")
    rows: list[dict[str, str]] = []
    for match in re.finditer(r"\{[^{}\n]*\bstatus\s*:\s*[\"']published[\"'][^{}\n]*\}", raw):
        obj = match.group(0)
        def field(name: str) -> str:
            found = re.search(rf"\b{re.escape(name)}\s*:\s*[\"']([^\"']*)[\"']", obj)
            return found.group(1) if found else ""
        ticker = field("ticker")
        if ticker:
            market = norm_market(field("market"))
            rows.append({"ticker": norm_ticker(ticker, market), "name": field("name"), "market": market})
    return rows


def find_value(html: str, field_id: str) -> str:
    pat = rf'<input[^>]+id=["\']{re.escape(field_id)}["\'][^>]+value=["\']([^"\']+)["\']'
    m = re.search(pat, html, re.I)
    return m.group(1).strip() if m else ""


def fetch_us(target: dict[str, str], http: PoliteSession) -> tuple[dict[str, Any] | None, str]:
    ticker = target["ticker"].upper().replace(".US", "")
    profile = US_IR_PROFILES.get(ticker)
    if not profile:
        return None, "ir_profile_missing"
    try:
        # 验证公开 IR 页面；Q4 页面若被边缘防护拒绝，仍逐一验证官方 transcript/media URL。
        ir_status = "ok"
        try:
            http.request("GET", profile["ir_url"])
        except requests.RequestException as exc:
            ir_status = "ir_page_blocked:" + exc.__class__.__name__
        media_url = profile.get("media_url", "")
        media_final_url = ""
        if media_url:
            media_host = urlparse(media_url).netloc.lower()
            if media_host in MEDIA_LINK_ONLY_HOSTS or profile.get("media_kind") in LINK_ONLY_MEDIA_KINDS:
                media_final_url = media_url
            else:
                media = http.request("GET", media_url)
                media_final_url = media.url
        transcript_url = profile.get("transcript_url", "")
        transcript_final_url = ""
        transcript = ""
        if transcript_url:
            transcript_res = http.request("GET", transcript_url)
            transcript_final_url = transcript_res.url
            transcript = extract_transcript(transcript_res)
            if len(transcript) < 200:
                return None, "transcript_empty"
        if not media_final_url and not transcript:
            return None, "official_media_or_transcript_missing"
        return {
            "type": "earnings_call", "form": "earnings_call", "date": profile["date"],
            "title": profile["title"], "url": profile["ir_url"],
            "media_url": media_final_url, "media_kind": profile.get("media_kind", "audio_webcast"),
            "transcript_url": transcript_final_url, "transcript": transcript,
            "ticker": target["ticker"], "market": "US", "name": target["name"],
            "tier": "会议", "source": "company_ir", "source_level": "S级一手",
            "ir_check": ir_status,
        }, "ok"
    except PermissionError as exc:
        return None, str(exc)
    except (requests.RequestException, RuntimeError, ValueError) as exc:
        return None, f"us_fetch_failed:{exc.__class__.__name__}:{exc}"


def fetch_hk(target: dict[str, str], http: PoliteSession) -> tuple[dict[str, Any] | None, str]:
    ticker = norm_ticker(target["ticker"], "HK")
    profile = HK_IR_PROFILES.get(ticker)
    if not profile:
        return None, "ir_profile_missing"
    try:
        http.request("GET", profile["ir_url"])
        media = http.request("GET", profile["media_url"])
        presentation_url = profile.get("presentation_url", "")
        presentation_final_url = ""
        if presentation_url:
            presentation = http.request("GET", presentation_url)
            presentation_final_url = presentation.url
        return {
            "type": "earnings_call", "form": "earnings_call", "date": profile["date"],
            "title": profile["title"], "url": profile["ir_url"],
            "media_url": media.url, "media_kind": profile.get("media_kind", "audio_webcast"),
            "transcript_url": profile.get("transcript_url", ""), "transcript": "",
            "presentation_url": presentation_final_url,
            "ticker": target["ticker"], "market": "HK", "name": target["name"],
            "tier": "会议", "source": "company_ir", "source_level": "S级一手",
        }, "ok"
    except PermissionError as exc:
        return None, str(exc)
    except requests.RequestException as exc:
        return None, f"hk_fetch_failed:{exc.__class__.__name__}:{exc}"


def fetch_cn(target: dict[str, str], http: PoliteSession) -> tuple[dict[str, Any] | None, str]:
    ticker = re.sub(r"\D", "", target["ticker"])
    company_page = f"https://ir.p5w.net/c/{ticker}"
    try:
        page = http.request("GET", company_page)
        company_id = find_value(page.text, "companyBaseinfoId")
        if not company_id:
            return None, "p5w_company_not_found"
        res = http.request(
            "POST", "https://ir.p5w.net/company/getCompanyRoadS.shtml",
            data={"companyBaseinfoId": company_id}, headers={"Referer": page.url},
        )
        payload = res.json()
        items = payload.get("obj", []) if payload.get("success") else []
        earnings = [x for x in items if "业绩说明会" in (x.get("roadshowTitle") or "")]
        if not earnings:
            return fetch_cn_cs(target, http, "p5w_no_public_earnings_replay")
        item = sorted(earnings, key=lambda x: x.get("roadshowDateStr") or "", reverse=True)[0]
        media_url = item.get("roadshowActiveHis") or item.get("outPcUrl") or item.get("roadshowUrl")
        if not media_url:
            return None, "p5w_replay_url_missing"
        return {
            "type": "earnings_call", "form": "earnings_call",
            "date": item.get("roadshowDateStr") or "",
            "title": item.get("roadshowTitle") or f"{target['name']}业绩说明会",
            "url": company_page, "media_url": media_url, "media_kind": "public_replay_page",
            "transcript_url": "", "transcript": "",
            "ticker": target["ticker"], "market": "CN", "name": target["name"],
            "tier": "会议", "source": "全景网公开路演厅", "source_level": "S级一手",
            "has_replay": bool(item.get("hasReplay")),
        }, "ok"
    except PermissionError as exc:
        return None, str(exc)
    except (requests.RequestException, ValueError, KeyError) as exc:
        return fetch_cn_cs(target, http, f"p5w_fetch_failed:{exc.__class__.__name__}:{exc}")


def fetch_cn_cs(target: dict[str, str], http: PoliteSession, previous: str) -> tuple[dict[str, Any] | None, str]:
    """全景无命中时，查中证路演中心公开业绩说明会列表。"""
    ticker = re.sub(r"\D", "", target["ticker"])
    pages = ["https://www.cs.com.cn/roadshow/yjsmh/list.html", "https://www.cs.com.cn/roadshow/szse/"]
    try:
        for page_url in pages:
            page = http.request("GET", page_url)
            for match in re.finditer(r"(?is)<a\b[^>]*href=[\"']([^\"']+)[\"'][^>]*>(.*?)</a>", page.text):
                title = clean_html(match.group(2))
                if ticker not in title or "业绩说明会" not in title:
                    continue
                context = clean_html(page.text[max(0, match.start() - 180):match.end() + 180])
                date_match = re.search(r"20\d{2}[-/]\d{2}[-/]\d{2}", context)
                media_url = urljoin(page.url, match.group(1))
                return {
                    "type": "earnings_call", "form": "earnings_call",
                    "date": date_match.group(0).replace("/", "-") if date_match else "",
                    "title": title, "url": page.url, "media_url": media_url,
                    "media_kind": "public_replay_page", "transcript_url": "", "transcript": "",
                    "ticker": target["ticker"], "market": "CN", "name": target["name"],
                    "tier": "会议", "source": "中证路演中心", "source_level": "S级一手",
                }, "ok"
        return None, previous + ";cs_no_public_earnings_replay"
    except PermissionError as exc:
        return None, previous + ";" + str(exc)
    except requests.RequestException as exc:
        return None, previous + f";cs_fetch_failed:{exc.__class__.__name__}:{exc}"


def dedupe_key(row: dict[str, Any]) -> str:
    return "|".join(str(row.get(k, "")) for k in ("type", "date", "url", "media_url"))


def append_jsonl(path: Path, row: dict[str, Any]) -> bool:
    path.parent.mkdir(parents=True, exist_ok=True)
    existing: set[str] = set()
    if path.exists():
        for line in path.read_text(encoding="utf-8", errors="replace").splitlines():
            try: existing.add(dedupe_key(json.loads(line)))
            except json.JSONDecodeError: pass
    if dedupe_key(row) in existing:
        return False
    with path.open("a", encoding="utf-8") as f:
        f.write(json.dumps(row, ensure_ascii=False) + "\n")
    return True


def needs_asr(row: dict[str, Any]) -> bool:
    if row.get("transcript"):
        return False
    media_url = str(row.get("media_url", "")).lower()
    media_kind = str(row.get("media_kind", "")).lower()
    return media_kind.startswith("audio") or media_url.endswith((".mp3", ".m4a", ".wav"))


def main() -> int:
    home = Path.home()
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--tickers", nargs="*", help="仅跑指定 ticker")
    parser.add_argument("--judgments-dir", type=Path, default=Path(os.getenv("JUDGMENTS_DIR", "judgments")))
    parser.add_argument("--data-js", type=Path, default=Path("/var/www/html/ai-map/data.js"),
                        help="judgments 目录不在本机时使用的生产 published 镜像")
    parser.add_argument("--watchlist", type=Path, default=home / "disclosure-pipeline/scripts/watchlist-disclosure.json")
    parser.add_argument("--by-company", type=Path, default=home / "disclosure-pipeline/by_company")
    parser.add_argument("--log-dir", type=Path, default=home / "agent-worker/logs/earnings-media")
    parser.add_argument("--asr-queue", type=Path, default=home / "disclosure-pipeline/asr_todo/earnings_media.jsonl")
    parser.add_argument("--delay", type=float, default=DEFAULT_DELAY)
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()

    published = load_published(args.judgments_dir) if args.judgments_dir.is_dir() else []
    target_source = "judgments"
    if not published:
        published = load_datajs_published(args.data_js)
        target_source = "production_datajs_judgments_mirror"
    if not published and args.tickers:
        published = load_watchlist(args.watchlist)
        target_source = "watchlist_name_lookup_only"
    wanted = {x.upper() for x in (args.tickers or [])}
    targets = [x for x in published if not wanted or x["ticker"].upper() in wanted]
    # 指定 ticker 不在快照时仍给出明确 miss，不静默漏掉。
    known = {x["ticker"].upper() for x in targets}
    for ticker in sorted(wanted - known):
        if ticker in US_IR_PROFILES:
            market = "US"
        elif norm_ticker(ticker, "HK") in HK_IR_PROFILES:
            market = "HK"
            ticker = norm_ticker(ticker, "HK")
        else:
            market = "UNKNOWN"
        targets.append({"ticker": ticker, "name": ticker, "market": market})
    unique_targets: list[dict[str, str]] = []
    seen_targets: set[tuple[str, str]] = set()
    for target in targets:
        key = (target["ticker"].upper(), norm_market(target["market"]))
        if key in seen_targets:
            continue
        seen_targets.add(key)
        unique_targets.append(target)
    targets = unique_targets

    now = dt.datetime.now(dt.timezone(dt.timedelta(hours=8))).isoformat(timespec="seconds")
    http = PoliteSession(args.delay)
    report: list[dict[str, Any]] = []
    for target in targets:
        market = norm_market(target["market"])
        if market == "US": record, reason = fetch_us(target, http)
        elif market == "CN": record, reason = fetch_cn(target, http)
        elif market == "HK": record, reason = fetch_hk(target, http)
        else: record, reason = None, "market_unknown"
        if record:
            record["fetched_at"] = now
            written = False if args.dry_run else append_jsonl(args.by_company / f"{target['ticker']}.jsonl", record)
            if needs_asr(record) and not args.dry_run:
                append_jsonl(args.asr_queue, {"type": "earnings_media_asr", "ticker": target["ticker"],
                                               "media_url": record["media_url"], "queued_at": now})
            report.append({"ticker": target["ticker"], "market": market, "status": "ok",
                           "written": written, "title": record["title"], "date": record["date"],
                           "media_url": record["media_url"], "transcript_url": record.get("transcript_url", ""),
                           "transcript_chars": len(record.get("transcript", ""))})
        else:
            report.append({"ticker": target["ticker"], "market": market, "status": "miss", "reason": reason})

    args.log_dir.mkdir(parents=True, exist_ok=True)
    run_file = args.log_dir / (dt.date.today().isoformat() + ".jsonl")
    run = {"run_at": now, "target_source": target_source, "dry_run": args.dry_run, "results": report}
    if not args.dry_run:
        with run_file.open("a", encoding="utf-8") as f: f.write(json.dumps(run, ensure_ascii=False) + "\n")
    print(json.dumps(run, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
