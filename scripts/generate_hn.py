#!/usr/bin/env python3
"""Generate an 800x480 E Ink Spectra 6 optimized Hacker News newspaper PNG."""

from __future__ import annotations

import concurrent.futures
import html
import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
import urllib.parse
import urllib.request
from datetime import datetime, timezone, timedelta
from pathlib import Path

CST = timezone(timedelta(hours=8))
REPO_ROOT = Path(__file__).resolve().parent.parent
OUT_PNG = REPO_ROOT / "docs" / "hn.png"
OUT_HTML_PREVIEW = REPO_ROOT / "docs" / "hn_render.html"

USER_AGENT = "e1002-news-board/1.0 (+https://github.com/Lightningxxl/e1002-news-board)"


def http_get_json(url: str, timeout: int = 15):
    req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return json.loads(resp.read().decode("utf-8"))


def fetch_hn_algolia(limit: int = 12) -> list[dict]:
    data = http_get_json(
        f"https://hn.algolia.com/api/v1/search?tags=front_page&hitsPerPage={limit + 4}"
    )
    stories: list[dict] = []
    for hit in data.get("hits", []):
        title = (hit.get("title") or "").strip()
        if not title:
            continue
        url = hit.get("url") or f"https://news.ycombinator.com/item?id={hit.get('objectID', '')}"
        stories.append(
            {
                "id": str(hit.get("objectID", "")),
                "title": title,
                "url": url,
                "domain": extract_domain(url),
                "points": int(hit.get("points") or 0),
                "comments": int(hit.get("num_comments") or 0),
                "author": hit.get("author") or "",
                "created_at": int(hit.get("created_at_i") or time.time()),
            }
        )
        if len(stories) >= limit:
            break
    return stories


def fetch_hn_firebase(limit: int = 12) -> list[dict]:
    ids = http_get_json("https://hacker-news.firebaseio.com/v0/topstories.json")[: limit + 8]

    def load_item(item_id: int):
        try:
            return http_get_json(
                f"https://hacker-news.firebaseio.com/v0/item/{item_id}.json", timeout=10
            )
        except Exception:
            return None

    with concurrent.futures.ThreadPoolExecutor(max_workers=8) as pool:
        items = list(pool.map(load_item, ids))

    stories: list[dict] = []
    for item in items:
        if not item or item.get("type") != "story" or not item.get("title"):
            continue
        url = item.get("url") or f"https://news.ycombinator.com/item?id={item.get('id', '')}"
        stories.append(
            {
                "id": str(item.get("id", "")),
                "title": item["title"].strip(),
                "url": url,
                "domain": extract_domain(url),
                "points": int(item.get("score") or 0),
                "comments": int(item.get("descendants") or 0),
                "author": item.get("by") or "",
                "created_at": int(item.get("time") or time.time()),
            }
        )
        if len(stories) >= limit:
            break
    return stories


def fetch_stories(limit: int = 11) -> list[dict]:
    try:
        stories = fetch_hn_algolia(limit=limit)
        if stories:
            return stories
    except Exception as exc:
        print(f"Algolia HN fetch failed ({exc}), falling back to Firebase API...", file=sys.stderr)
    return fetch_hn_firebase(limit=limit)


def extract_domain(url: str) -> str:
    try:
        host = urllib.parse.urlparse(url).netloc.lower()
        if host.startswith("www."):
            host = host[4:]
        if host == "news.ycombinator.com":
            return "ycombinator.com"
        return host or "ycombinator.com"
    except Exception:
        return "ycombinator.com"


def relative_age(ts: int, now_ts: float) -> str:
    delta = max(0, int(now_ts - ts))
    if delta < 3600:
        return f"{max(1, delta // 60)}m"
    if delta < 86400:
        return f"{delta // 3600}h"
    return f"{delta // 86400}d"


def detect_tag(title: str) -> tuple[str, str]:
    """Return (label, css_class) if the story has a well-known HN prefix."""
    upper = title.upper()
    if upper.startswith("SHOW HN:"):
        return "SHOW HN", "tag-blue"
    if upper.startswith("ASK HN:"):
        return "ASK HN", "tag-green"
    if upper.startswith("LAUNCH HN:"):
        return "LAUNCH", "tag-red"
    return "", ""


def strip_prefix(title: str, tag: str) -> str:
    if not tag:
        return title
    for prefix in ("Show HN:", "Ask HN:", "Launch HN:"):
        if title.lower().startswith(prefix.lower()):
            return title[len(prefix) :].strip()
    return title


def build_html(stories: list[dict], now: datetime) -> str:
    now_ts = now.timestamp()
    edition = "MORNING EDITION" if now.hour < 14 else "EVENING EDITION"
    date_str = now.strftime("%a, %b %d, %Y · %H:%M CST").upper()

    lead = stories[0]
    rest = stories[1:11]
    left_col = rest[:5]
    right_col = rest[5:10]

    lead_tag, lead_tag_cls = detect_tag(lead["title"])
    lead_title = html.escape(strip_prefix(lead["title"], lead_tag))
    lead_tag_html = f'<span class="pill {lead_tag_cls}">{lead_tag}</span>' if lead_tag else ""

    def render_story(rank: int, s: dict) -> str:
        tag, tag_cls = detect_tag(s["title"])
        clean_title = html.escape(strip_prefix(s["title"], tag))
        tag_html = f'<span class="mini-tag {tag_cls}">{tag}</span> ' if tag else ""
        rank_cls = "rank rank-hot" if rank <= 3 or s["points"] >= 300 else "rank"
        age = relative_age(s["created_at"], now_ts)
        return f"""
        <article class="story">
          <div class="{rank_cls}">{rank:02d}</div>
          <div class="story-body">
            <h3 class="story-title">{tag_html}{clean_title}</h3>
            <div class="story-meta">
              <span class="domain">{html.escape(s['domain'])}</span>
              <span class="dot">·</span>
              <span class="pts">▲ {s['points']}</span>
              <span class="dot">·</span>
              <span class="cmts">{s['comments']} cmts</span>
              <span class="dot">·</span>
              <span class="age">{age}</span>
            </div>
          </div>
        </article>"""

    left_html = "\n".join(render_story(i + 2, s) for i, s in enumerate(left_col))
    right_html = "\n".join(render_story(i + 7, s) for i, s in enumerate(right_col))

    return f"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8" />
<title>The Hacker News — E1002 Edition</title>
<style>
  /* E Ink Spectra 6 palette: pure white, pure black, red, yellow, blue, green */
  :root {{
    --paper: #FFFFFF;
    --ink: #000000;
    --red: #D60000;
    --yellow: #FFE600;
    --blue: #0033CC;
    --green: #007A00;
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
    font-family: -apple-system, BlinkMacSystemFont, "Helvetica Neue", "Inter", "Liberation Sans", Arial, sans-serif;
    -webkit-font-smoothing: none;
    text-rendering: geometricPrecision;
  }}

  .page {{
    width: 800px;
    height: 480px;
    padding: 10px 14px 8px 14px;
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
    margin-bottom: 8px;
  }}

  .brand {{
    display: flex;
    align-items: center;
    gap: 10px;
  }}

  .y-badge {{
    width: 32px;
    height: 32px;
    background: var(--red);
    color: var(--paper);
    font-weight: 900;
    font-size: 22px;
    line-height: 28px;
    text-align: center;
    border: 2px solid var(--ink);
  }}

  .masthead-title {{
    font-family: Georgia, "Times New Roman", "Liberation Serif", serif;
    font-size: 27px;
    font-weight: 900;
    letter-spacing: 0.4px;
    line-height: 1;
  }}

  .edition-meta {{
    text-align: right;
    display: flex;
    flex-direction: column;
    align-items: flex-end;
    gap: 3px;
  }}

  .edition-pill {{
    background: var(--yellow);
    color: var(--ink);
    border: 1.5px solid var(--ink);
    font-size: 10px;
    font-weight: 900;
    letter-spacing: 0.9px;
    padding: 1px 7px;
  }}

  .date-line {{
    font-size: 11px;
    font-weight: 700;
    letter-spacing: 0.3px;
  }}

  /* Lead Story (#1) */
  .lead {{
    border: 2px solid var(--ink);
    border-left: 7px solid var(--red);
    padding: 7px 11px;
    margin-bottom: 8px;
    background: var(--paper);
  }}

  .lead-top {{
    display: flex;
    align-items: center;
    justify-content: space-between;
    margin-bottom: 3px;
  }}

  .lead-badges {{
    display: flex;
    align-items: center;
    gap: 6px;
  }}

  .lead-kicker {{
    background: var(--red);
    color: var(--paper);
    font-size: 10px;
    font-weight: 900;
    letter-spacing: 0.8px;
    padding: 1px 6px;
  }}

  .pill {{
    font-size: 10px;
    font-weight: 800;
    padding: 1px 6px;
    color: var(--paper);
  }}

  .lead-stats {{
    font-size: 12px;
    font-weight: 800;
    display: flex;
    gap: 8px;
    align-items: center;
  }}

  .lead-title {{
    font-family: Georgia, "Times New Roman", "Liberation Serif", serif;
    font-size: 18px;
    font-weight: 900;
    line-height: 1.18;
    display: -webkit-box;
    -webkit-line-clamp: 2;
    -webkit-box-orient: vertical;
    overflow: hidden;
  }}

  /* Two-Column Grid */
  .columns {{
    display: grid;
    grid-template-columns: 1fr 1fr;
    gap: 14px;
    flex: 1;
    min-height: 0;
  }}

  .col {{
    display: flex;
    flex-direction: column;
  }}

  .col-right {{
    border-left: 1.5px solid var(--ink);
    padding-left: 14px;
  }}

  .story {{
    flex: 1;
    display: flex;
    align-items: center;
    gap: 8px;
    padding: 3px 0;
    border-bottom: 1px solid var(--ink);
  }}

  .story:last-child {{
    border-bottom: none;
  }}

  .rank {{
    width: 24px;
    flex-shrink: 0;
    font-family: Georgia, "Times New Roman", serif;
    font-size: 16px;
    font-weight: 900;
    line-height: 1.1;
    color: var(--ink);
  }}

  .rank-hot {{
    color: var(--red);
  }}

  .story-body {{
    flex: 1;
    min-width: 0;
  }}

  .story-title {{
    font-size: 13.5px;
    font-weight: 800;
    line-height: 1.22;
    margin-bottom: 2px;
    display: -webkit-box;
    -webkit-line-clamp: 2;
    -webkit-box-orient: vertical;
    overflow: hidden;
  }}

  .mini-tag {{
    display: inline-block;
    font-size: 9.5px;
    font-weight: 900;
    padding: 0px 4px;
    vertical-align: 1px;
    color: var(--paper);
  }}

  .tag-blue {{ background: var(--blue); }}
  .tag-green {{ background: var(--green); }}
  .tag-red {{ background: var(--red); }}

  .story-meta {{
    font-size: 11px;
    font-weight: 700;
    white-space: nowrap;
    overflow: hidden;
    text-overflow: ellipsis;
  }}

  .domain {{ color: var(--blue); }}
  .pts {{ color: var(--green); font-weight: 800; }}
  .cmts {{ color: var(--ink); }}
  .age {{ color: var(--ink); }}
  .dot {{ margin: 0 3px; }}

  /* Footer */
  .footer {{
    border-top: 2px solid var(--ink);
    margin-top: 5px;
    padding-top: 4px;
    display: flex;
    justify-content: space-between;
    font-size: 11px;
    font-weight: 800;
    letter-spacing: 0.2px;
  }}

  .footer-accent {{
    color: var(--red);
  }}
</style>
</head>
<body>
  <div class="page">
    <header class="masthead">
      <div class="brand">
        <div class="y-badge">Y</div>
        <div class="masthead-title">HACKER NEWS DAILY</div>
      </div>
      <div class="edition-meta">
        <div class="edition-pill">{edition}</div>
        <div class="date-line">{date_str}</div>
      </div>
    </header>

    <section class="lead">
      <div class="lead-top">
        <div class="lead-badges">
          <span class="lead-kicker">#1 TOP STORY</span>
          {lead_tag_html}
        </div>
        <div class="lead-stats">
          <span class="domain">{html.escape(lead['domain'])}</span>
          <span class="pts">▲ {lead['points']} pts</span>
          <span>{lead['comments']} comments</span>
          <span>{relative_age(lead['created_at'], now_ts)} ago</span>
        </div>
      </div>
      <h2 class="lead-title">{lead_title}</h2>
    </section>

    <main class="columns">
      <div class="col col-left">
        {left_html}
      </div>
      <div class="col col-right">
        {right_html}
      </div>
    </main>

    <footer class="footer">
      <span><span class="footer-accent">■</span> reTerminal E1002 · Spectra 6 Front Page</span>
      <span>Auto Refresh: 08:00 &amp; 20:00 CST · news.ycombinator.com</span>
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


def main() -> None:
    now = datetime.now(CST)
    stories = fetch_stories(limit=11)
    if len(stories) < 2:
        raise RuntimeError("Failed to fetch Hacker News stories.")
    page_html = build_html(stories, now)
    render_png(page_html, OUT_PNG)
    print(f"Rendered {OUT_PNG} ({OUT_PNG.stat().st_size} bytes) with {len(stories)} stories.")


if __name__ == "__main__":
    main()
