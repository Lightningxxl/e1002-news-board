#!/usr/bin/env python3
"""Generate an 800x480 1:1 pixel-exact E Ink Spectra 6 US/CN Tech & Markets Daily PNG.

Output pixels are strictly quantized to the 6 theoretical Spectra 6 RGB values
expected by esp32-photoframe's `check_processed_png()` fast path:
  Black  (0, 0, 0)
  White  (255, 255, 255)
  Yellow (255, 255, 0)
  Red    (255, 0, 0)
  Blue   (0, 0, 255)
  Green  (0, 255, 0)
"""

from __future__ import annotations

import html
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import time
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET
from datetime import datetime, timedelta, timezone
from pathlib import Path

from PIL import Image

CST = timezone(timedelta(hours=8))
REPO_ROOT = Path(__file__).resolve().parent.parent
OUT_PNG = REPO_ROOT / "docs" / "hn.png"
OUT_HTML_PREVIEW = REPO_ROOT / "docs" / "hn_render.html"

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
    """Fetch regular + extended-hours quotes from CNBC in one batch call."""
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
    """Fetch A-share / HK / US fallback quotes from Tencent Finance."""
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
    """Build 4 US market rows + 4 CN market rows with regular + extended-hours %."""
    cnbc = fetch_cnbc_quotes(["SPY", "QQQ", "SOXX", "XLY", "KWEB", ".SSEC", ".HSI"])
    qq = fetch_tencent_quotes(
        ["usSPY", "usQQQ", "usSOXX", "usXLY", "usKWEB", "sh000300", "hkHSTECH", "sh512480", "sz159928"]
    )

    def pick_us(label: str, ticker: str, cnbc_sym: str, qq_sym: str) -> dict:
        q = cnbc.get(cnbc_sym) or qq.get(qq_sym) or {}
        return {
            "label": label,
            "ticker": ticker,
            "day_pct": q.get("day_pct"),
            "ext_label": q.get("ext_label", ""),
            "ext_pct": q.get("ext_pct"),
            "last": q.get("last", ""),
        }

    def pick_cn(label: str, ticker: str, qq_sym: str, cnbc_sym: str | None = None) -> dict:
        q_qq = qq.get(qq_sym) or {}
        q_cnbc = cnbc.get(cnbc_sym) if cnbc_sym else None
        src = q_cnbc if (q_cnbc and q_cnbc.get("day_pct") is not None) else q_qq
        return {
            "label": label,
            "ticker": ticker,
            "day_pct": src.get("day_pct"),
            "ext_label": src.get("ext_label", ""),
            "ext_pct": src.get("ext_pct"),
            "last": src.get("last", ""),
        }

    us_rows = [
        pick_us("标普500", "SPY", "SPY", "usSPY"),
        pick_us("纳指100", "QQQ", "QQQ", "usQQQ"),
        pick_us("半导体", "SOXX", "SOXX", "usSOXX"),
        pick_us("美消费", "XLY", "XLY", "usXLY"),
    ]

    cn_rows = [
        pick_cn("沪深300", "CSI", "sh000300"),
        pick_cn("中概科技", "KWEB", "usKWEB", cnbc_sym="KWEB"),
        pick_cn("A半导体", "512480", "sh512480"),
        pick_cn("A股消费", "159928", "sz159928"),
    ]

    return us_rows, cn_rows


def fetch_hn_top(limit: int = 2) -> list[dict]:
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
            pts = int(hit.get("points") or 0)
            items.append({"tag": "HN", "tag_cls": "tag-red", "title": title, "meta": f"▲{pts}"})
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
            title = (el.findtext("title") or "").strip()
            title = re.sub(r"\s+", " ", title)
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
    """Fetch top Chinese global finance & tech headlines from WallstreetCN."""
    url = (
        "https://api-one-wscn.awtmt.com/apiv1/content/information-flow"
        f"?channel=global&accept=article&limit={limit + 6}"
    )
    try:
        data = json.loads(http_get(url, timeout=12).decode("utf-8"))
        items = []
        for entry in data.get("data", {}).get("items", []):
            res = entry.get("resource") or {}
            title = (res.get("title") or "").strip()
            if not title or len(title) < 6:
                continue
            # Clean trailing spaces or promotional prefixes
            title = re.sub(r"\s+", " ", title)
            tag = "华尔街"
            tag_cls = "tag-blue"
            if any(k in title for k in ("芯片", "AI", "人工智能", "英伟达", "谷歌", "苹果", "华为", "特斯拉", "半导体", "算力", "模型", "OpenAI", "Anthropic", "TPU", "GPU")):
                tag = "科技"
                tag_cls = "tag-red"
            elif any(k in title for k in ("美联储", "美债", "黄金", "A股", "港股", "纳斯达克", "标普", "央行", "利率", "财报", "债")):
                tag = "金融"
                tag_cls = "tag-blue"
            items.append({"tag": tag, "tag_cls": tag_cls, "title": title, "meta": ""})
            if len(items) >= limit:
                break
        return items
    except Exception as exc:
        print(f"[WARN] WSCN fetch failed: {exc}", file=sys.stderr)
        return []


def fetch_news() -> tuple[list[dict], list[dict]]:
    # Left column: 2 Hacker News + 2 CNBC Tech/Finance
    hn = fetch_hn_top(limit=2)
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
    us_news: list[dict] = []
    us_news.extend(hn[:2])
    # Pick 1 CNBC Tech + 1 CNBC Finance (deduplicated)
    seen_us = {x["title"] for x in us_news}
    for src in (cnbc_tech, cnbc_fin):
        for item in src:
            if item["title"] not in seen_us:
                us_news.append(item)
                seen_us.add(item["title"])
                break
    # Fill up to 4 if any source was short
    for src in (cnbc_tech, cnbc_fin, hn):
        for item in src:
            if len(us_news) >= 4:
                break
            if item["title"] not in seen_us:
                us_news.append(item)
                seen_us.add(item["title"])

    # Right column: 4 Chinese Finance & Tech headlines (WSCN primary, Google News fallback)
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

    return us_news[:4], cn_news[:4]


def fmt_pct_html(pct: float | None, prefix: str = "") -> str:
    if pct is None:
        return '<span class="muted">--</span>'
    sign = "+" if pct > 0 else ""
    cls = "up" if pct > 0 else ("down" if pct < 0 else "flat")
    arrow = "▲" if pct > 0 else ("▼" if pct < 0 else "")
    pfx = f'<span class="ext-pfx">{prefix}</span>' if prefix else ""
    return f'<span class="{cls}">{pfx}{arrow}{sign}{pct:.2f}%</span>'


def render_market_rows(rows: list[dict], show_last_if_no_ext: bool = False) -> str:
    parts = []
    for r in rows:
        day_html = fmt_pct_html(r["day_pct"])
        if r["ext_pct"] is not None:
            ext_html = fmt_pct_html(r["ext_pct"], prefix=f"{r['ext_label']} ")
        elif show_last_if_no_ext and r["last"]:
            try:
                val = float(r["last"])
                txt = f"{val:,.0f}" if val >= 100 else f"{val:.3f}"
            except ValueError:
                txt = r["last"]
            ext_html = f'<span class="flat">{html.escape(txt)}</span>'
        else:
            ext_html = '<span class="flat">--</span>'

        parts.append(
            f"""
            <tr>
              <td class="m-name">{html.escape(r['label'])} <span class="m-sym">{html.escape(r['ticker'])}</span></td>
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


def build_html(
    us_rows: list[dict],
    cn_rows: list[dict],
    us_news: list[dict],
    cn_news: list[dict],
    now: datetime,
) -> str:
    weekdays = ["周一", "周二", "周三", "周四", "周五", "周六", "周日"]
    edition = "早报 MORNING" if now.hour < 14 else "晚报 EVENING"
    date_str = f"{now.strftime('%m/%d')} {weekdays[now.weekday()]} {now.strftime('%H:%M')} CST"

    return f"""<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="utf-8" />
<title>US·CN Tech &amp; Markets Daily — E1002</title>
<style>
  /* Exact 6-color theoretical Spectra 6 palette */
  :root {{
    --paper: #FFFFFF;
    --ink: #000000;
    --red: #FF0000;
    --yellow: #FFFF00;
    --blue: #0000FF;
    --green: #00FF00;
  }}

  * {{
    box-sizing: border-box;
    margin: 0;
    padding: 0;
  }}

  html, body {{
    width: 800px;
    height: 480px;
    overflow: hidden;
    background: var(--paper);
    color: var(--ink);
    font-family: "PingFang SC", "Noto Sans CJK SC", "Hiragino Sans GB", "Helvetica Neue", Arial, sans-serif;
    -webkit-font-smoothing: antialiased;
  }}

  .page {{
    width: 800px;
    height: 480px;
    padding: 9px 13px 7px 13px;
    display: flex;
    flex-direction: column;
  }}

  /* Masthead */
  .masthead {{
    display: flex;
    align-items: center;
    justify-content: space-between;
    border-bottom: 3px solid var(--ink);
    padding-bottom: 6px;
    margin-bottom: 7px;
  }}

  .brand {{
    display: flex;
    align-items: center;
    gap: 9px;
  }}

  .badge {{
    background: var(--red);
    color: var(--paper);
    border: 2px solid var(--ink);
    font-size: 15px;
    font-weight: 900;
    padding: 2px 7px;
    letter-spacing: 0.5px;
  }}

  .title {{
    font-size: 22px;
    font-weight: 900;
    letter-spacing: 0.3px;
    line-height: 1;
  }}

  .meta-right {{
    display: flex;
    align-items: center;
    gap: 8px;
  }}

  .edition-pill {{
    background: var(--yellow);
    color: var(--ink);
    border: 2px solid var(--ink);
    font-size: 12px;
    font-weight: 900;
    padding: 1px 7px;
  }}

  .date-str {{
    font-size: 13px;
    font-weight: 900;
  }}

  /* Markets Strip (US + CN side-by-side) */
  .markets {{
    display: grid;
    grid-template-columns: 1fr 1fr;
    gap: 10px;
    margin-bottom: 8px;
  }}

  .m-box {{
    border: 2px solid var(--ink);
  }}

  .m-table {{
    width: 100%;
    border-collapse: collapse;
    table-layout: fixed;
  }}

  .m-table thead th {{
    background: var(--ink);
    color: var(--paper);
    font-size: 11.5px;
    font-weight: 900;
    padding: 3px 6px;
    text-align: left;
  }}

  .m-table thead th.th-num {{
    text-align: right;
  }}

  .m-table tbody td {{
    padding: 3.5px 6px;
    font-size: 13.5px;
    font-weight: 700;
    border-top: 1px solid var(--ink);
    white-space: nowrap;
  }}

  .m-name {{
    width: 40%;
  }}

  .m-sym {{
    font-size: 11px;
    font-weight: 900;
    color: var(--blue);
  }}

  .m-day {{
    width: 28%;
    text-align: right;
    font-variant-numeric: tabular-nums;
  }}

  .m-ext {{
    width: 32%;
    text-align: right;
    font-variant-numeric: tabular-nums;
  }}

  .ext-pfx {{
    color: var(--ink);
    font-size: 11px;
    font-weight: 900;
  }}

  /* International convention: Green UP, Red DOWN */
  .up {{ color: var(--green); font-weight: 900; }}
  .down {{ color: var(--red); font-weight: 900; }}
  .flat {{ color: var(--ink); font-weight: 800; }}
  .muted {{ color: var(--ink); }}

  /* News Two-Column Section */
  .news-grid {{
    display: grid;
    grid-template-columns: 1fr 1fr;
    gap: 14px;
    flex: 1;
    min-height: 0;
  }}

  .news-col {{
    display: flex;
    flex-direction: column;
    min-height: 0;
  }}

  .news-col-right {{
    border-left: 2px solid var(--ink);
    padding-left: 14px;
  }}

  .sec-head {{
    display: flex;
    align-items: center;
    justify-content: space-between;
    border-bottom: 2px solid var(--ink);
    padding-bottom: 3px;
    margin-bottom: 4px;
  }}

  .sec-title {{
    font-size: 13.5px;
    font-weight: 900;
    letter-spacing: 0.3px;
  }}

  .sec-dot-red {{ color: var(--red); }}
  .sec-dot-blue {{ color: var(--blue); }}

  .news-list {{
    display: flex;
    flex-direction: column;
    flex: 1;
    min-height: 0;
  }}

  .news-item {{
    flex: 1;
    display: -webkit-box;
    -webkit-line-clamp: 2;
    -webkit-box-orient: vertical;
    overflow: hidden;
    padding: 5px 0;
    border-bottom: 1px solid var(--ink);
    font-size: 14.5px;
    font-weight: 700;
    line-height: 1.32;
  }}

  .news-item:last-child {{
    border-bottom: none;
  }}

  .n-tag {{
    display: inline-block;
    font-size: 11px;
    font-weight: 900;
    padding: 1px 5px;
    margin-right: 4px;
    vertical-align: 1px;
    color: var(--paper);
    line-height: 1.15;
  }}

  .tag-red {{ background: var(--red); }}
  .tag-blue {{ background: var(--blue); }}
  .tag-black {{ background: var(--ink); }}

  .n-meta {{
    font-size: 11.5px;
    font-weight: 900;
    color: var(--red);
    margin-right: 4px;
  }}

  /* Footer */
  .footer {{
    border-top: 2px solid var(--ink);
    margin-top: 4px;
    padding-top: 3px;
    display: flex;
    justify-content: space-between;
    font-size: 11.5px;
    font-weight: 800;
  }}
</style>
</head>
<body>
  <div class="page">
    <header class="masthead">
      <div class="brand">
        <span class="badge">全球</span>
        <span class="title">中美科技金融日报</span>
      </div>
      <div class="meta-right">
        <span class="edition-pill">{edition}</span>
        <span class="date-str">{date_str}</span>
      </div>
    </header>

    <section class="markets">
      <div class="m-box">
        <table class="m-table">
          <thead>
            <tr>
              <th style="width:40%">US 美股 &amp; 板块</th>
              <th class="th-num" style="width:28%">日内 DAY</th>
              <th class="th-num" style="width:32%">盘后/盘前</th>
            </tr>
          </thead>
          <tbody>
            {render_market_rows(us_rows, show_last_if_no_ext=False)}
          </tbody>
        </table>
      </div>
      <div class="m-box">
        <table class="m-table">
          <thead>
            <tr>
              <th style="width:40%">CN 中国 &amp; 板块</th>
              <th class="th-num" style="width:28%">日内 DAY</th>
              <th class="th-num" style="width:32%">盘后/点位</th>
            </tr>
          </thead>
          <tbody>
            {render_market_rows(cn_rows, show_last_if_no_ext=True)}
          </tbody>
        </table>
      </div>
    </section>

    <main class="news-grid">
      <section class="news-col">
        <div class="sec-head">
          <span class="sec-title"><span class="sec-dot-red">■</span> US 科技 &amp; 金融要闻</span>
          <span class="m-sym">HN · CNBC</span>
        </div>
        <div class="news-list">
          {render_news_items(us_news)}
        </div>
      </section>

      <section class="news-col news-col-right">
        <div class="sec-head">
          <span class="sec-title"><span class="sec-dot-blue">■</span> CN 财经 &amp; 科技要闻</span>
          <span class="m-sym">华尔街见闻</span>
        </div>
        <div class="news-list">
          {render_news_items(cn_news)}
        </div>
      </section>
    </main>

    <footer class="footer">
      <span><span class="up">▲绿涨</span> <span class="down">▼红跌</span> · Spectra 6 1:1 Crisp</span>
      <span>Auto Refresh: 08:00 &amp; 20:00 CST · E1002</span>
    </footer>
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
    """Map an antialiased RGB pixel cleanly to one of the 6 exact Spectra 6 colors."""
    cmax = max(r, g, b)
    cmin = min(r, g, b)
    chroma = cmax - cmin

    # Neutral grays (text & borders). Threshold 158 keeps CJK strokes crisp and open.
    if chroma < 48:
        lum = (r * 299 + g * 587 + b * 114) // 1000
        return BLACK if lum < 158 else WHITE

    # Yellow box
    if r >= 165 and g >= 165 and b <= 165:
        return YELLOW
    # Blue text/badge
    if b > r + 45 and b > g + 45:
        return WHITE if (r > 135 and g > 135) else BLUE
    # Red text/badge
    if r > g + 45 and r > b + 45:
        return WHITE if (g > 135 and b > 135) else RED
    # Green text/badge (pure #00FF00 renders as rich dark green on Spectra 6)
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
    """Convert rendered PNG in-place to an exact 6-color non-interlaced RGB PNG."""
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


def render_png(html_content: str, out_png: Path) -> None:
    chrome = find_chrome()
    out_png.parent.mkdir(parents=True, exist_ok=True)
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


def main() -> None:
    now = datetime.now(CST)
    us_rows, cn_rows = fetch_markets()
    us_news, cn_news = fetch_news()
    page_html = build_html(us_rows, cn_rows, us_news, cn_news, now)
    render_png(page_html, OUT_PNG)
    print(
        f"Rendered 1:1 Spectra 6 board {OUT_PNG} ({OUT_PNG.stat().st_size} bytes): "
        f"{len(us_rows)} US + {len(cn_rows)} CN markets, {len(us_news)} US + {len(cn_news)} CN news."
    )


if __name__ == "__main__":
    main()
