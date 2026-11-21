#!/usr/bin/env python3
"""Generate an 800x480 1:1 pixel-exact E Ink Spectra 6 full-day rotating board.

Three daily modes (auto-selected by Asia/Shanghai hour, or via --mode):
  - morning (05:00-11:59): US/CN Markets (index points + intraday + AH/PRE) + US & CN Tech/Finance News
  - midday  (12:00-18:59): Compact US/CN Market Strip + Hacker News Top 6 Deep-Read
  - evening (19:00-04:59): Minimalist Daily Calendar + Generative Bauhaus/Risograph Art Print + Market Ticker

Every pixel in the output PNG is strictly one of the 6 theoretical Spectra 6 RGB
values expected by esp32-photoframe's `check_processed_png()` fast path:
  Black (0,0,0), White (255,255,255), Yellow (255,255,0),
  Red (255,0,0), Blue (0,0,255), Green (0,255,0).
"""

from __future__ import annotations

import argparse
import hashlib
import html
import json
import os
import random
import re
import shutil
import subprocess
import sys
import tempfile
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET
from datetime import datetime, timedelta, timezone
from pathlib import Path

from PIL import Image

CST = timezone(timedelta(hours=8))
REPO_ROOT = Path(__file__).resolve().parent.parent
DOCS_DIR = REPO_ROOT / "docs"
OUT_PNG = DOCS_DIR / "hn.png"
OUT_HTML_PREVIEW = DOCS_DIR / "hn_render.html"

UA = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
    "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/130.0.0.0 Safari/537.36"
)

BLACK = (0, 0, 0)
WHITE = (255, 255, 255)
YELLOW = (255, 255, 0)
RED = (255, 0, 0)
BLUE = (0, 0, 255)
GREEN = (0, 255, 0)
EXACT_PALETTE = {BLACK, WHITE, YELLOW, RED, BLUE, GREEN}

DAILY_QUOTES = [
    ("Simple can be harder than complex.", "Steve Jobs"),
    ("Simplicity is prerequisite for reliability.", "Edsger Dijkstra"),
    ("In the short run, the market is a voting machine; in the long run, a weighing machine.", "Benjamin Graham"),
    ("Stay hungry, stay foolish.", "Whole Earth Catalog"),
    ("Make things as simple as possible, but not simpler.", "Albert Einstein"),
    ("The best way to predict the future is to invent it.", "Alan Kay"),
    ("Risk comes from not knowing what you are doing.", "Warren Buffett"),
    ("First, solve the problem. Then, write the code.", "John Johnson"),
    ("Good design is as little design as possible.", "Dieter Rams"),
    ("Compound interest is the eighth wonder of the world.", "Unknown"),
    ("Clarity affords focus.", "Thomas Leonard"),
    ("Move slowly enough to see what actually matters.", "E1002 Notes"),
]


def http_get(url: str, timeout: int = 12) -> bytes:
    req = urllib.request.Request(url, headers={"User-Agent": UA})
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return resp.read()


def parse_pct(raw: str | float | None) -> float | None:
    if raw is None:
        return None
    s = str(raw).strip().replace("%", "").replace("+", "").replace(",", "")
    if not s or s in ("--", "UNCH", "None"):
        return None
    try:
        return float(s)
    except ValueError:
        return None


def fetch_cnbc_quotes(symbols: list[str]) -> dict[str, dict]:
    sym_str = "|".join(symbols)
    url = (
        "https://quote.cnbc.com/quote-html-webservice/restQuote/symbolType/symbol"
        f"?symbols={urllib.parse.quote(sym_str, safe='|.')}"
        "&requestMethod=itv&noform=1&partnerId=2&fund=1&exthrs=1&output=json&events=1"
    )
    out: dict[str, dict] = {}
    try:
        data = json.loads(http_get(url, timeout=12).decode("utf-8"))
        quotes = data.get("FormattedQuoteResult", {}).get("FormattedQuote", [])
        if isinstance(quotes, dict):
            quotes = [quotes]
        for q in quotes:
            sym = q.get("symbol", "")
            ext = q.get("ExtendedMktQuote") or {}
            ext_type_raw = (ext.get("type") or "").upper()
            ext_label = "AH" if "POST" in ext_type_raw else ("PRE" if "PRE" in ext_type_raw else "")
            out[sym] = {
                "last": str(q.get("last") or "").replace(",", ""),
                "day_pct": parse_pct(q.get("change_pct")),
                "ext_label": ext_label,
                "ext_pct": parse_pct(ext.get("change_pct")),
            }
    except Exception as exc:
        print(f"[WARN] CNBC quote fetch failed: {exc}", file=sys.stderr)
    return out


def fetch_tencent_quotes(codes: list[str]) -> dict[str, dict]:
    url = f"https://qt.gtimg.cn/q={','.join(codes)}"
    out: dict[str, dict] = {}
    try:
        txt = http_get(url, timeout=12).decode("gbk", errors="replace")
        for line in txt.strip().splitlines():
            if "=" not in line:
                continue
            key, val = line.split("=", 1)
            code = key.removeprefix("v_").strip()
            fields = val.strip('";').split("~")
            if len(fields) > 32:
                out[code] = {
                    "last": fields[3],
                    "day_pct": parse_pct(fields[32]),
                    "ext_label": "",
                    "ext_pct": None,
                }
    except Exception as exc:
        print(f"[WARN] Tencent quote fetch failed: {exc}", file=sys.stderr)
    return out


def fetch_markets() -> tuple[list[dict], list[dict]]:
    cnbc = fetch_cnbc_quotes([".SPX", ".IXIC", ".SOX", "SPY", "QQQ", "SOXX", "XLY", "KWEB"])
    qq = fetch_tencent_quotes(
        ["usSPY", "usQQQ", "usSOXX", "usXLY", "usKWEB", "sh000300", "hkHSTECH", "sh512480", "sz159928"]
    )

    def pick_us_index(label: str, idx_sym: str, etf_sym: str, qq_sym: str) -> dict:
        idx = cnbc.get(idx_sym) or {}
        etf = cnbc.get(etf_sym) or qq.get(qq_sym) or {}
        last = idx.get("last") or etf.get("last", "")
        day_pct = idx.get("day_pct") if idx.get("day_pct") is not None else etf.get("day_pct")
        return {
            "label": label,
            "last": last,
            "day_pct": day_pct,
            "ext_label": etf.get("ext_label", ""),
            "ext_pct": etf.get("ext_pct"),
        }

    def pick_cn(label: str, qq_sym: str, cnbc_sym: str | None = None) -> dict:
        q_qq = qq.get(qq_sym) or {}
        q_cnbc = cnbc.get(cnbc_sym) if cnbc_sym else None
        src = q_cnbc if (q_cnbc and q_cnbc.get("day_pct") is not None) else q_qq
        return {
            "label": label,
            "last": src.get("last", ""),
            "day_pct": src.get("day_pct"),
            "ext_label": src.get("ext_label", ""),
            "ext_pct": src.get("ext_pct"),
        }

    us_rows = [
        pick_us_index("标普500", ".SPX", "SPY", "usSPY"),
        pick_us_index("纳斯达克", ".IXIC", "QQQ", "usQQQ"),
        pick_us_index("费城半导", ".SOX", "SOXX", "usSOXX"),
        pick_us_index("美消费XLY", "XLY", "XLY", "usXLY"),
    ]

    cn_rows = [
        pick_cn("沪深300", "sh000300"),
        pick_cn("中概KWEB", "usKWEB", cnbc_sym="KWEB"),
        pick_cn("A半导体", "sh512480"),
        pick_cn("A股消费", "sz159928"),
    ]

    return us_rows, cn_rows


def extract_domain(url: str) -> str:
    try:
        host = urllib.parse.urlparse(url).netloc.lower()
        if host.startswith("www."):
            host = host[4:]
        return "ycombinator.com" if host in ("", "news.ycombinator.com") else host
    except Exception:
        return "ycombinator.com"


def fetch_hn_top(limit: int = 6) -> list[dict]:
    try:
        data = json.loads(
            http_get(
                f"https://hn.algolia.com/api/v1/search?tags=front_page&hitsPerPage={limit + 4}",
                timeout=12,
            ).decode("utf-8")
        )
        items = []
        for hit in data.get("hits", []):
            title = (hit.get("title") or "").strip()
            if not title:
                continue
            url = hit.get("url") or f"https://news.ycombinator.com/item?id={hit.get('objectID', '')}"
            pts = int(hit.get("points") or 0)
            cmts = int(hit.get("num_comments") or 0)
            items.append(
                {
                    "tag": "HN",
                    "tag_cls": "tag-red",
                    "title": title,
                    "domain": extract_domain(url),
                    "points": pts,
                    "comments": cmts,
                    "meta": f"▲{pts}",
                }
            )
            if len(items) >= limit:
                break
        return items
    except Exception as exc:
        print(f"[WARN] HN fetch failed: {exc}", file=sys.stderr)
        return []


def fetch_rss_titles(url: str, tag: str, tag_cls: str, limit: int = 3) -> list[dict]:
    try:
        root = ET.fromstring(http_get(url, timeout=12))
        items = []
        seen: set[str] = set()
        for el in root.findall(".//item"):
            title = re.sub(r"\s+", " ", (el.findtext("title") or "").strip())
            if not title or title in seen:
                continue
            seen.add(title)
            items.append({"tag": tag, "tag_cls": tag_cls, "title": title, "meta": ""})
            if len(items) >= limit:
                break
        return items
    except Exception as exc:
        print(f"[WARN] RSS fetch failed ({url}): {exc}", file=sys.stderr)
        return []


def fetch_wscn_cn(limit: int = 4) -> list[dict]:
    url = (
        "https://api-one-wscn.awtmt.com/apiv1/content/information-flow"
        f"?channel=global&accept=article&limit={limit + 6}"
    )
    try:
        data = json.loads(http_get(url, timeout=12).decode("utf-8"))
        items = []
        for entry in data.get("data", {}).get("items", []):
            res = entry.get("resource") or {}
            title = re.sub(r"\s+", " ", (res.get("title") or "").strip())
            if not title or len(title) < 6:
                continue
            tag, tag_cls = "华尔街", "tag-blue"
            if any(
                k in title
                for k in (
                    "芯片", "AI", "人工智能", "英伟达", "谷歌", "苹果", "华为",
                    "特斯拉", "半导体", "算力", "模型", "OpenAI", "Anthropic", "TPU", "GPU",
                )
            ):
                tag, tag_cls = "科技", "tag-red"
            elif any(
                k in title
                for k in ("美联储", "美债", "黄金", "A股", "港股", "纳斯达克", "标普", "央行", "利率", "财报", "债")
            ):
                tag, tag_cls = "金融", "tag-blue"
            items.append({"tag": tag, "tag_cls": tag_cls, "title": title, "meta": ""})
            if len(items) >= limit:
                break
        return items
    except Exception as exc:
        print(f"[WARN] WSCN fetch failed: {exc}", file=sys.stderr)
        return []


def fetch_news() -> tuple[list[dict], list[dict], list[dict]]:
    hn_all = fetch_hn_top(limit=6)
    cnbc_tech = fetch_rss_titles(
        "https://search.cnbc.com/rs/search/combinedcms/view.xml?partnerId=wrss01&id=19854910",
        "TECH",
        "tag-blue",
        limit=2,
    )
    cnbc_fin = fetch_rss_titles(
        "https://search.cnbc.com/rs/search/combinedcms/view.xml?partnerId=wrss01&id=10000664",
        "FIN",
        "tag-black",
        limit=2,
    )
    us_news: list[dict] = list(hn_all[:2])
    seen_us = {x["title"] for x in us_news}
    for src in (cnbc_tech, cnbc_fin):
        for item in src:
            if item["title"] not in seen_us:
                us_news.append(item)
                seen_us.add(item["title"])
                break
    for src in (cnbc_tech, cnbc_fin, hn_all):
        for item in src:
            if len(us_news) >= 4:
                break
            if item["title"] not in seen_us:
                us_news.append(item)
                seen_us.add(item["title"])

    cn_news = fetch_wscn_cn(limit=4)
    if len(cn_news) < 4:
        g_biz = fetch_rss_titles(
            "https://news.google.com/rss/headlines/section/topic/BUSINESS?hl=zh-CN&gl=CN&ceid=CN:zh-Hans",
            "财经",
            "tag-blue",
            limit=4,
        )
        seen_cn = {x["title"] for x in cn_news}
        for item in g_biz:
            title = item["title"].rsplit(" - ", 1)[0].strip()
            if title not in seen_cn:
                cn_news.append({**item, "title": title})
                seen_cn.add(title)
            if len(cn_news) >= 4:
                break

    return us_news[:4], cn_news[:4], hn_all[:6]


def fmt_price(raw: str) -> str:
    if not raw:
        return "--"
    try:
        v = float(str(raw).replace(",", ""))
        if v >= 1000:
            return f"{v:,.0f}"
        if v >= 10:
            return f"{v:.2f}"
        return f"{v:.3f}"
    except ValueError:
        return str(raw)


def fmt_pct_html(pct: float | None, prefix: str = "") -> str:
    if pct is None:
        return '<span class="flat">--</span>'
    sign = "+" if pct > 0 else ""
    cls = "up" if pct > 0 else ("down" if pct < 0 else "flat")
    pfx = f'<span class="ext-pfx">{prefix}</span>' if prefix else ""
    return f'<span class="{cls}">{pfx}{sign}{pct:.2f}%</span>'


def render_market_rows(rows: list[dict]) -> str:
    parts = []
    for r in rows:
        px_str = fmt_price(r["last"])
        day_html = fmt_pct_html(r["day_pct"])
        ext_html = (
            fmt_pct_html(r["ext_pct"], prefix=f"{r['ext_label']} ")
            if r["ext_pct"] is not None
            else '<span class="flat">--</span>'
        )
        parts.append(
            f"""
            <tr>
              <td class="m-name">{html.escape(r['label'])}</td>
              <td class="m-px">{html.escape(px_str)}</td>
              <td class="m-day">{day_html}</td>
              <td class="m-ext">{ext_html}</td>
            </tr>"""
        )
    return "\n".join(parts)


def render_news_items(items: list[dict]) -> str:
    parts = []
    for item in items:
        meta_html = f' <span class="n-meta">{html.escape(item["meta"])}</span>' if item.get("meta") else ""
        parts.append(
            f"""
            <article class="news-item">
              <span class="n-tag {item['tag_cls']}">{html.escape(item['tag'])}</span>{meta_html}
              <span class="n-title">{html.escape(item['title'])}</span>
            </article>"""
        )
    return "\n".join(parts)


BASE_CSS = """
  :root {
    --paper: #FFFFFF;
    --ink: #000000;
    --red: #FF0000;
    --yellow: #FFFF00;
    --blue: #0000FF;
    --green: #00FF00;
  }
  * { box-sizing: border-box; margin: 0; padding: 0; }
  html, body {
    width: 800px; height: 480px; overflow: hidden;
    background: var(--paper); color: var(--ink);
    font-family: "PingFang SC", "Noto Sans CJK SC", "Hiragino Sans GB", "Helvetica Neue", Arial, sans-serif;
    -webkit-font-smoothing: antialiased;
  }
  .page {
    width: 800px; height: 480px;
    padding: 9px 13px 7px 13px;
    display: flex; flex-direction: column;
  }
  .masthead {
    display: flex; align-items: center; justify-content: space-between;
    border-bottom: 3px solid var(--ink);
    padding-bottom: 6px; margin-bottom: 7px;
  }
  .brand { display: flex; align-items: center; gap: 9px; }
  .badge {
    background: var(--red); color: var(--paper); border: 2px solid var(--ink);
    font-size: 15px; font-weight: 900; padding: 2px 7px;
  }
  .badge-blue { background: var(--blue); }
  .title { font-size: 22px; font-weight: 900; line-height: 1; }
  .meta-right { display: flex; align-items: center; gap: 8px; }
  .edition-pill {
    background: var(--yellow); color: var(--ink); border: 2px solid var(--ink);
    font-size: 12px; font-weight: 900; padding: 1px 7px;
  }
  .date-str { font-size: 13px; font-weight: 900; }
  .up { color: var(--green); font-weight: 900; }
  .down { color: var(--red); font-weight: 900; }
  .flat { color: var(--ink); font-weight: 800; }
  .ext-pfx { color: var(--ink); font-size: 11px; font-weight: 900; }
  .footer {
    border-top: 2px solid var(--ink); margin-top: 4px; padding-top: 3px;
    display: flex; justify-content: space-between;
    font-size: 11.5px; font-weight: 800;
  }
"""


def build_morning_html(
    us_rows: list[dict],
    cn_rows: list[dict],
    us_news: list[dict],
    cn_news: list[dict],
    now: datetime,
) -> str:
    weekdays = ["周一", "周二", "周三", "周四", "周五", "周六", "周日"]
    date_str = f"{now.strftime('%m/%d')} {weekdays[now.weekday()]} {now.strftime('%H:%M')} CST"

    return f"""<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="utf-8" />
<style>
{BASE_CSS}
  .markets {{ display: grid; grid-template-columns: 1fr 1fr; gap: 10px; margin-bottom: 8px; }}
  .m-box {{ border: 2px solid var(--ink); }}
  .m-table {{ width: 100%; border-collapse: collapse; table-layout: fixed; }}
  .m-table thead th {{
    background: var(--ink); color: var(--paper);
    font-size: 11.5px; font-weight: 900; padding: 3px 5px; text-align: left;
  }}
  .m-table thead th.th-num {{ text-align: right; }}
  .m-table tbody td {{
    padding: 3.5px 5px; font-size: 13px; font-weight: 700;
    border-top: 1px solid var(--ink); white-space: nowrap;
  }}
  .m-name {{ width: 28%; }}
  .m-px {{ width: 22%; text-align: right; color: var(--blue); font-weight: 800; font-variant-numeric: tabular-nums; }}
  .m-day {{ width: 23%; text-align: right; font-variant-numeric: tabular-nums; }}
  .m-ext {{ width: 27%; text-align: right; font-variant-numeric: tabular-nums; }}
  .m-sym {{ font-size: 11px; font-weight: 900; color: var(--blue); }}

  .news-grid {{ display: grid; grid-template-columns: 1fr 1fr; gap: 14px; flex: 1; min-height: 0; }}
  .news-col {{ display: flex; flex-direction: column; min-height: 0; }}
  .news-col-right {{ border-left: 2px solid var(--ink); padding-left: 14px; }}
  .sec-head {{
    display: flex; align-items: center; justify-content: space-between;
    border-bottom: 2px solid var(--ink); padding-bottom: 3px; margin-bottom: 4px;
  }}
  .sec-title {{ font-size: 13.5px; font-weight: 900; }}
  .sec-dot-red {{ color: var(--red); }}
  .sec-dot-blue {{ color: var(--blue); }}
  .news-list {{ display: flex; flex-direction: column; flex: 1; min-height: 0; }}
  .news-item {{
    flex: 1; display: -webkit-box; -webkit-line-clamp: 2; -webkit-box-orient: vertical;
    overflow: hidden; padding: 5px 0; border-bottom: 1px solid var(--ink);
    font-size: 14.5px; font-weight: 700; line-height: 1.32;
  }}
  .news-item:last-child {{ border-bottom: none; }}
  .n-tag {{
    display: inline-block; font-size: 11px; font-weight: 900;
    padding: 1px 5px; margin-right: 4px; vertical-align: 1px; color: var(--paper); line-height: 1.15;
  }}
  .tag-red {{ background: var(--red); }}
  .tag-blue {{ background: var(--blue); }}
  .tag-black {{ background: var(--ink); }}
  .n-meta {{ font-size: 11.5px; font-weight: 900; color: var(--red); margin-right: 4px; }}
</style>
</head>
<body>
  <div class="page">
    <header class="masthead">
      <div class="brand">
        <span class="badge">早报</span>
        <span class="title">中美科技金融日报</span>
      </div>
      <div class="meta-right">
        <span class="edition-pill">08:00 MORNING</span>
        <span class="date-str">{date_str}</span>
      </div>
    </header>

    <section class="markets">
      <div class="m-box">
        <table class="m-table">
          <thead>
            <tr>
              <th style="width:28%">US 美股</th>
              <th class="th-num" style="width:22%">点数</th>
              <th class="th-num" style="width:23%">日内</th>
              <th class="th-num" style="width:27%">盘后/前</th>
            </tr>
          </thead>
          <tbody>{render_market_rows(us_rows)}</tbody>
        </table>
      </div>
      <div class="m-box">
        <table class="m-table">
          <thead>
            <tr>
              <th style="width:28%">CN 中国</th>
              <th class="th-num" style="width:22%">点数</th>
              <th class="th-num" style="width:23%">日内</th>
              <th class="th-num" style="width:27%">盘后/前</th>
            </tr>
          </thead>
          <tbody>{render_market_rows(cn_rows)}</tbody>
        </table>
      </div>
    </section>

    <main class="news-grid">
      <section class="news-col">
        <div class="sec-head">
          <span class="sec-title"><span class="sec-dot-red">■</span> US 科技 &amp; 金融要闻</span>
          <span class="m-sym">HN · CNBC</span>
        </div>
        <div class="news-list">{render_news_items(us_news)}</div>
      </section>
      <section class="news-col news-col-right">
        <div class="sec-head">
          <span class="sec-title"><span class="sec-dot-blue">■</span> CN 财经 &amp; 科技要闻</span>
          <span class="m-sym">华尔街见闻</span>
        </div>
        <div class="news-list">{render_news_items(cn_news)}</div>
      </section>
    </main>

    <footer class="footer">
      <span><span class="up">▲绿涨</span> <span class="down">▼红跌</span> · 08:00 早报 / 13:00 午报 / 20:00 画报</span>
      <span>CNBC · 腾讯财经 · HN · 华尔街见闻</span>
    </footer>
  </div>
</body>
</html>
"""


def build_midday_html(
    us_rows: list[dict],
    cn_rows: list[dict],
    hn_items: list[dict],
    now: datetime,
) -> str:
    """13:00 Midday Edition: Compact 8-tile market ticker + Hacker News Top 6 large-type deep read."""
    weekdays = ["周一", "周二", "周三", "周四", "周五", "周六", "周日"]
    date_str = f"{now.strftime('%m/%d')} {weekdays[now.weekday()]} {now.strftime('%H:%M')} CST"

    tiles = []
    for r in us_rows + cn_rows:
        px = fmt_price(r["last"])
        day_html = fmt_pct_html(r["day_pct"])
        ext_html = (
            f'<div class="t-ext">{fmt_pct_html(r["ext_pct"], prefix=r["ext_label"] + " ")}</div>'
            if r["ext_pct"] is not None
            else '<div class="t-ext"><span class="flat">日内收盘/午盘</span></div>'
        )
        tiles.append(
            f"""
            <div class="tile">
              <div class="t-top">
                <span class="t-name">{html.escape(r['label'])}</span>
                <span class="t-px">{html.escape(px)}</span>
              </div>
              <div class="t-mid">{day_html}</div>
              {ext_html}
            </div>"""
        )

    left_hn = hn_items[:3]
    right_hn = hn_items[3:6]

    def render_hn_card(rank: int, s: dict) -> str:
        r_cls = "hn-rank hn-hot" if rank <= 2 else "hn-rank"
        return f"""
        <article class="hn-card">
          <div class="{r_cls}">{rank:02d}</div>
          <div class="hn-body">
            <div class="hn-title">{html.escape(s['title'])}</div>
            <div class="hn-meta">
              <span class="hn-dom">{html.escape(s['domain'])}</span>
              <span>·</span>
              <span class="hn-pts">▲{s['points']}</span>
              <span>·</span>
              <span>{s['comments']} cmts</span>
            </div>
          </div>
        </article>"""

    left_html = "\n".join(render_hn_card(i + 1, s) for i, s in enumerate(left_hn))
    right_html = "\n".join(render_hn_card(i + 4, s) for i, s in enumerate(right_hn))

    return f"""<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="utf-8" />
<style>
{BASE_CSS}
  .tiles {{
    display: grid; grid-template-columns: repeat(4, 1fr); gap: 6px;
    margin-bottom: 8px;
  }}
  .tile {{
    border: 2px solid var(--ink); padding: 4px 6px;
    display: flex; flex-direction: column; gap: 1px;
  }}
  .t-top {{ display: flex; justify-content: space-between; align-items: baseline; }}
  .t-name {{ font-size: 12px; font-weight: 900; }}
  .t-px {{ font-size: 12px; font-weight: 900; color: var(--blue); font-variant-numeric: tabular-nums; }}
  .t-mid {{ font-size: 14px; font-weight: 900; font-variant-numeric: tabular-nums; line-height: 1.15; }}
  .t-ext {{ font-size: 10.5px; font-weight: 800; font-variant-numeric: tabular-nums; }}

  .hn-sec-head {{
    display: flex; justify-content: space-between; align-items: center;
    border-bottom: 2px solid var(--ink); padding-bottom: 3px; margin-bottom: 4px;
  }}
  .hn-sec-title {{ font-size: 14px; font-weight: 900; }}
  .hn-badge {{ background: var(--red); color: var(--paper); padding: 0 5px; margin-right: 6px; }}
  .hn-grid {{ display: grid; grid-template-columns: 1fr 1fr; gap: 14px; flex: 1; min-height: 0; }}
  .hn-col {{ display: flex; flex-direction: column; }}
  .hn-col-right {{ border-left: 2px solid var(--ink); padding-left: 14px; }}
  .hn-card {{
    flex: 1; display: flex; align-items: center; gap: 9px;
    padding: 4px 0; border-bottom: 1px solid var(--ink);
  }}
  .hn-card:last-child {{ border-bottom: none; }}
  .hn-rank {{ font-family: Georgia, serif; font-size: 20px; font-weight: 900; width: 26px; flex-shrink: 0; }}
  .hn-hot {{ color: var(--red); }}
  .hn-body {{ flex: 1; min-width: 0; }}
  .hn-title {{
    font-size: 15px; font-weight: 800; line-height: 1.24; margin-bottom: 2px;
    display: -webkit-box; -webkit-line-clamp: 2; -webkit-box-orient: vertical; overflow: hidden;
  }}
  .hn-meta {{ font-size: 12px; font-weight: 800; display: flex; gap: 5px; white-space: nowrap; overflow: hidden; }}
  .hn-dom {{ color: var(--blue); }}
  .hn-pts {{ color: var(--red); font-weight: 900; }}
</style>
</head>
<body>
  <div class="page">
    <header class="masthead">
      <div class="brand">
        <span class="badge badge-blue">午报</span>
        <span class="title">午间盘面 × HN</span>
      </div>
      <div class="meta-right">
        <span class="edition-pill">13:00 MIDDAY</span>
        <span class="date-str">{date_str}</span>
      </div>
    </header>

    <section class="tiles">
      {"".join(tiles)}
    </section>

    <div class="hn-sec-head">
      <span class="hn-sec-title"><span class="hn-badge">Y</span>Hacker News 午休深度精选</span>
      <span class="t-px">news.ycombinator.com</span>
    </div>

    <main class="hn-grid">
      <div class="hn-col">{left_html}</div>
      <div class="hn-col hn-col-right">{right_html}</div>
    </main>

    <footer class="footer">
      <span><span class="up">▲绿涨</span> <span class="down">▼红跌</span> · 午间档 13:00 · 晚 20:00 切换艺术画报</span>
      <span>reTerminal E1002 · Spectra 6 Crisp</span>
    </footer>
  </div>
</body>
</html>
"""


def generate_bauhaus_svg(seed_str: str) -> str:
    """Deterministic daily Bauhaus/Swiss geometric print using pure Spectra 6 colors."""
    digest = int(hashlib.sha256(seed_str.encode("utf-8")).hexdigest()[:12], 16)
    rng = random.Random(digest)
    palette = ["#FF0000", "#FFFF00", "#0000FF", "#00FF00", "#000000"]
    shapes: list[str] = []

    # 3x3 modular Swiss grid (390 x 354 canvas)
    cols, rows = 3, 3
    cw, ch = 130, 118
    for r in range(rows):
        for c in range(cols):
            x, y = c * cw, r * ch
            bg = rng.choice(["#FFFFFF", "#FFFFFF", "#FFFF00", "#0000FF", "#FF0000", "#000000"])
            fg = rng.choice([p for p in palette if p != bg] + ["#FFFFFF"])
            fg2 = rng.choice([p for p in palette if p not in (bg, fg)])
            shapes.append(f'<rect x="{x}" y="{y}" width="{cw}" height="{ch}" fill="{bg}" stroke="#000000" stroke-width="3"/>')
            kind = rng.randint(0, 5)
            if kind == 0:
                # Full circle
                shapes.append(
                    f'<circle cx="{x + cw // 2}" cy="{y + ch // 2}" r="{min(cw, ch) // 2 - 12}" fill="{fg}" stroke="#000000" stroke-width="3"/>'
                )
            elif kind == 1:
                # Diagonal split triangle
                pts = rng.choice([
                    f"{x},{y} {x+cw},{y} {x},{y+ch}",
                    f"{x+cw},{y} {x+cw},{y+ch} {x},{y+ch}",
                    f"{x},{y} {x+cw},{y+ch} {x},{y+ch}",
                ])
                shapes.append(f'<polygon points="{pts}" fill="{fg}" stroke="#000000" stroke-width="3"/>')
            elif kind == 2:
                # Quarter arc wedge
                corner = rng.randint(0, 3)
                rad = min(cw, ch) - 10
                if corner == 0:
                    d = f"M {x} {y} L {x+rad} {y} A {rad} {rad} 0 0 1 {x} {y+rad} Z"
                elif corner == 1:
                    d = f"M {x+cw} {y} L {x+cw} {y+rad} A {rad} {rad} 0 0 1 {x+cw-rad} {y} Z"
                elif corner == 2:
                    d = f"M {x+cw} {y+ch} L {x+cw-rad} {y+ch} A {rad} {rad} 0 0 1 {x+cw} {y+ch-rad} Z"
                else:
                    d = f"M {x} {y+ch} L {x} {y+ch-rad} A {rad} {rad} 0 0 1 {x+rad} {y+ch} Z"
                shapes.append(f'<path d="{d}" fill="{fg}" stroke="#000000" stroke-width="3"/>')
            elif kind == 3:
                # Concentric target
                cx, cy = x + cw // 2, y + ch // 2
                shapes.append(f'<circle cx="{cx}" cy="{cy}" r="42" fill="{fg}" stroke="#000000" stroke-width="3"/>')
                shapes.append(f'<circle cx="{cx}" cy="{cy}" r="20" fill="{fg2}" stroke="#000000" stroke-width="3"/>')
            elif kind == 4:
                # Bold horizontal or vertical bars
                for k in range(3):
                    shapes.append(
                        f'<rect x="{x + 14}" y="{y + 16 + k * 30}" width="{cw - 28}" height="16" fill="{fg if k % 2 == 0 else fg2}" stroke="#000000" stroke-width="2"/>'
                    )
            else:
                # Half circle + offset dot
                cx, cy = x + cw // 2, y + ch // 2
                shapes.append(f'<path d="M {x+14} {cy} A 51 51 0 0 1 {x+cw-14} {cy} Z" fill="{fg}" stroke="#000000" stroke-width="3"/>')
                shapes.append(f'<circle cx="{cx}" cy="{cy + 28}" r="14" fill="{fg2}" stroke="#000000" stroke-width="3"/>')

    return (
        '<svg xmlns="http://www.w3.org/2000/svg" width="390" height="354" viewBox="0 0 390 354">'
        + "".join(shapes)
        + '<rect x="1.5" y="1.5" width="387" height="351" fill="none" stroke="#000000" stroke-width="3"/>'
        + "</svg>"
    )


def build_evening_html(
    us_rows: list[dict],
    cn_rows: list[dict],
    now: datetime,
) -> str:
    """20:00 Evening Edition: Art Print Calendar + Year Progress + Compact Market Close Bar."""
    weekdays_cn = ["星期一", "星期二", "星期三", "星期四", "星期五", "星期六", "星期日"]
    weekdays_en = ["MONDAY", "TUESDAY", "WEDNESDAY", "THURSDAY", "FRIDAY", "SATURDAY", "SUNDAY"]
    months_en = ["JAN", "FEB", "MAR", "APR", "MAY", "JUN", "JUL", "AUG", "SEP", "OCT", "NOV", "DEC"]

    day_of_year = now.timetuple().tm_yday
    is_leap = (now.year % 4 == 0 and now.year % 100 != 0) or (now.year % 400 == 0)
    total_days = 366 if is_leap else 365
    pct_year = round(day_of_year / total_days * 100)
    days_left = total_days - day_of_year

    quote_text, quote_author = DAILY_QUOTES[day_of_year % len(DAILY_QUOTES)]
    art_svg = generate_bauhaus_svg(now.strftime("%Y-%m-%d"))

    # Compact market close summary strip (4 key anchors: SPX, NASDAQ, SOX, CSI300)
    picks = [us_rows[0], us_rows[1], us_rows[2], cn_rows[0], cn_rows[1]]
    ticker_items = []
    for r in picks:
        ext = f' <span class="e-ext">{r["ext_label"]}{"+" if (r["ext_pct"] or 0) > 0 else ""}{r["ext_pct"]:.1f}%</span>' if r["ext_pct"] is not None else ""
        ticker_items.append(
            f'<span class="e-tick"><b>{html.escape(r["label"])}</b> '
            f'<span class="e-px">{html.escape(fmt_price(r["last"]))}</span> '
            f'{fmt_pct_html(r["day_pct"])}{ext}</span>'
        )

    return f"""<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="utf-8" />
<style>
{BASE_CSS}
  .ev-body {{
    display: grid; grid-template-columns: 370px 390px; gap: 14px;
    flex: 1; min-height: 0; align-items: stretch;
  }}
  .cal-panel {{
    display: flex; flex-direction: column; justify-content: space-between;
    border: 2px solid var(--ink); padding: 12px 14px;
  }}
  .cal-top {{
    display: flex; justify-content: space-between; align-items: center;
    border-bottom: 2px solid var(--ink); padding-bottom: 6px;
  }}
  .cal-month {{ font-size: 16px; font-weight: 900; letter-spacing: 1px; }}
  .cal-wk-tag {{
    background: var(--yellow); border: 2px solid var(--ink);
    font-size: 12px; font-weight: 900; padding: 1px 7px;
  }}
  .cal-day-wrap {{
    display: flex; align-items: baseline; justify-content: space-between;
    padding: 4px 0;
  }}
  .cal-day-num {{
    font-family: Georgia, "Times New Roman", serif;
    font-size: 92px; font-weight: 900; line-height: 0.95; color: var(--ink);
  }}
  .cal-day-side {{ text-align: right; }}
  .cal-wk-cn {{ font-size: 24px; font-weight: 900; color: var(--red); }}
  .cal-wk-en {{ font-size: 13px; font-weight: 900; letter-spacing: 0.8px; color: var(--blue); }}

  .prog-box {{
    border-top: 2px solid var(--ink); border-bottom: 2px solid var(--ink);
    padding: 8px 0;
  }}
  .prog-head {{
    display: flex; justify-content: space-between;
    font-size: 12.5px; font-weight: 900; margin-bottom: 5px;
  }}
  .prog-bar {{
    width: 100%; height: 14px; border: 2px solid var(--ink); background: var(--paper);
  }}
  .prog-fill {{
    height: 100%; width: {pct_year}%; background: var(--blue);
  }}

  .quote-box {{ padding-top: 6px; }}
  .quote-txt {{
    font-family: Georgia, "Times New Roman", serif;
    font-size: 15px; font-weight: 900; line-height: 1.3; margin-bottom: 4px;
  }}
  .quote-auth {{ font-size: 12px; font-weight: 900; color: var(--red); text-align: right; }}

  .art-panel {{
    display: flex; align-items: center; justify-content: center;
  }}

  .ticker-bar {{
    border: 2px solid var(--ink); background: var(--paper);
    margin-top: 8px; padding: 4px 8px;
    display: flex; justify-content: space-between; align-items: center;
    font-size: 11.5px; font-weight: 800; white-space: nowrap; overflow: hidden;
  }}
  .e-px {{ color: var(--blue); font-weight: 900; }}
  .e-ext {{ color: var(--ink); font-size: 10.5px; }}
</style>
</head>
<body>
  <div class="page">
    <header class="masthead">
      <div class="brand">
        <span class="badge">晚间画报</span>
        <span class="title">DAILY PRINT</span>
      </div>
      <div class="meta-right">
        <span class="edition-pill">20:00 EVENING</span>
        <span class="date-str">{now.strftime('%Y.%m.%d')}</span>
      </div>
    </header>

    <main class="ev-body">
      <section class="cal-panel">
        <div class="cal-top">
          <span class="cal-month">{months_en[now.month - 1]} {now.year}</span>
          <span class="cal-wk-tag">DAY {day_of_year} / {total_days}</span>
        </div>
        <div class="cal-day-wrap">
          <div class="cal-day-num">{now.day:02d}</div>
          <div class="cal-day-side">
            <div class="cal-wk-cn">{weekdays_cn[now.weekday()]}</div>
            <div class="cal-wk-en">{weekdays_en[now.weekday()]}</div>
          </div>
        </div>
        <div class="prog-box">
          <div class="prog-head">
            <span>{now.year} YEAR PROGRESS</span>
            <span>{pct_year}% · 余 {days_left} 天</span>
          </div>
          <div class="prog-bar"><div class="prog-fill"></div></div>
        </div>
        <div class="quote-box">
          <div class="quote-txt">“{html.escape(quote_text)}”</div>
          <div class="quote-auth">— {html.escape(quote_author)}</div>
        </div>
      </section>

      <section class="art-panel">
        {art_svg}
      </section>
    </main>

    <div class="ticker-bar">
      {"".join(ticker_items[:4])}
    </div>
  </div>
</body>
</html>
"""


def find_chrome() -> str:
    candidates = [
        os.environ.get("CHROME_BIN", ""),
        "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome",
        "/Applications/Chromium.app/Contents/MacOS/Chromium",
        shutil.which("google-chrome") or "",
        shutil.which("google-chrome-stable") or "",
        shutil.which("chromium") or "",
        shutil.which("chromium-browser") or "",
    ]
    for path in candidates:
        if path and os.path.exists(path):
            return path
    raise RuntimeError("No Chrome/Chromium binary found. Set CHROME_BIN env var.")


def snap_pixel_to_spectra6(r: int, g: int, b: int) -> tuple[int, int, int]:
    cmax = max(r, g, b)
    cmin = min(r, g, b)
    chroma = cmax - cmin

    if chroma < 48:
        lum = (r * 299 + g * 587 + b * 114) // 1000
        return BLACK if lum < 158 else WHITE

    if r >= 165 and g >= 165 and b <= 165:
        return YELLOW
    if b > r + 45 and b > g + 45:
        return WHITE if (r > 135 and g > 135) else BLUE
    if r > g + 45 and r > b + 45:
        return WHITE if (g > 135 and b > 135) else RED
    if g > r + 45 and g > b + 45:
        return WHITE if (r > 145 and b > 145) else GREEN

    best = WHITE
    best_dist = 10**9
    for pr, pg, pb in EXACT_PALETTE:
        d = (r - pr) ** 2 + (g - pg) ** 2 + (b - pb) ** 2
        if d < best_dist:
            best_dist = d
            best = (pr, pg, pb)
    return best


def quantize_to_spectra6(png_path: Path) -> None:
    im = Image.open(png_path).convert("RGB")
    if im.size != (800, 480):
        im = im.resize((800, 480), Image.Resampling.NEAREST)

    src = list(im.get_flattened_data() if hasattr(im, "get_flattened_data") else im.getdata())
    cache: dict[tuple[int, int, int], tuple[int, int, int]] = {}
    out: list[tuple[int, int, int]] = []
    for px in src:
        mapped = cache.get(px)
        if mapped is None:
            mapped = snap_pixel_to_spectra6(*px)
            cache[px] = mapped
        out.append(mapped)

    assert set(cache.values()) <= EXACT_PALETTE, "Unexpected color outside Spectra 6 palette"
    clean = Image.new("RGB", (800, 480))
    clean.putdata(out)
    clean.save(png_path, format="PNG", optimize=True, interlace=0)


def render_png(html_content: str, out_png: Path, save_html: bool = False) -> None:
    chrome = find_chrome()
    out_png.parent.mkdir(parents=True, exist_ok=True)
    if save_html:
        OUT_HTML_PREVIEW.write_text(html_content, encoding="utf-8")

    with tempfile.TemporaryDirectory() as tmpdir:
        tmp_html = Path(tmpdir) / "page.html"
        tmp_html.write_text(html_content, encoding="utf-8")
        cmd = [
            chrome,
            "--headless=new",
            "--disable-gpu",
            "--no-sandbox",
            "--disable-dev-shm-usage",
            "--hide-scrollbars",
            "--force-device-scale-factor=1",
            "--window-size=800,480",
            f"--screenshot={out_png}",
            tmp_html.as_uri(),
        ]
        subprocess.run(cmd, check=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE)

    quantize_to_spectra6(out_png)


def resolve_mode(mode_arg: str, now: datetime) -> str:
    if mode_arg in ("morning", "midday", "evening"):
        return mode_arg
    if 5 <= now.hour < 12:
        return "morning"
    if 12 <= now.hour < 19:
        return "midday"
    return "evening"


def main() -> None:
    parser = argparse.ArgumentParser(description="Generate E1002 Spectra 6 daily board PNG.")
    parser.add_argument(
        "--mode",
        choices=["auto", "morning", "midday", "evening", "all"],
        default=os.environ.get("BOARD_MODE", "auto"),
        help="Board layout mode (default: auto by CST hour; 'all' renders all 3 previews too).",
    )
    args = parser.parse_args()

    now = datetime.now(CST)
    us_rows, cn_rows = fetch_markets()
    us_news, cn_news, hn_items = fetch_news()

    if args.mode == "all":
        for m, html_fn in (
            ("morning", lambda: build_morning_html(us_rows, cn_rows, us_news, cn_news, now)),
            ("midday", lambda: build_midday_html(us_rows, cn_rows, hn_items, now)),
            ("evening", lambda: build_evening_html(us_rows, cn_rows, now)),
        ):
            p = DOCS_DIR / f"{m}.png"
            render_png(html_fn(), p)
            print(f"Rendered {p} ({p.stat().st_size} bytes)")
        # Also write current auto mode to OUT_PNG
        active = resolve_mode("auto", now)
        shutil.copyfile(DOCS_DIR / f"{active}.png", OUT_PNG)
        print(f"Active auto mode ({active}) copied to {OUT_PNG}")
        return

    mode = resolve_mode(args.mode, now)
    if mode == "morning":
        page_html = build_morning_html(us_rows, cn_rows, us_news, cn_news, now)
    elif mode == "midday":
        page_html = build_midday_html(us_rows, cn_rows, hn_items, now)
    else:
        page_html = build_evening_html(us_rows, cn_rows, now)

    render_png(page_html, OUT_PNG, save_html=True)
    # Also keep per-mode file updated for preview page
    shutil.copyfile(OUT_PNG, DOCS_DIR / f"{mode}.png")
    print(f"Rendered [{mode}] 1:1 Spectra 6 -> {OUT_PNG} ({OUT_PNG.stat().st_size} bytes)")


if __name__ == "__main__":
    main()
