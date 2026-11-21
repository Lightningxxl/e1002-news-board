#!/usr/bin/env python3
"""Generate an 800x480 1:1 pixel-exact E Ink Spectra 6 Hacker News newspaper PNG.

Every pixel in the output PNG is strictly one of the 6 theoretical Spectra 6 RGB
values expected by esp32-photoframe's `check_processed_png()` fast path:
  Black  (0, 0, 0)
  White  (255, 255, 255)
  Yellow (255, 255, 0)
  Red    (255, 0, 0)
  Blue   (0, 0, 255)
  Green  (0, 255, 0)

This bypasses on-device Floyd-Steinberg photo dithering completely so text and
color blocks render with zero speckles and crisp 1:1 edges.
"""

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
from datetime import datetime, timedelta, timezone
from pathlib import Path

from PIL import Image

CST = timezone(timedelta(hours=8))
REPO_ROOT = Path(__file__).resolve().parent.parent
OUT_PNG = REPO_ROOT / "docs" / "hn.png"
OUT_HTML_PREVIEW = REPO_ROOT / "docs" / "hn_render.html"

USER_AGENT = "e1002-news-board/1.1 (+https://github.com/Lightningxxl/e1002-news-board)"

BLACK = (0, 0, 0)
WHITE = (255, 255, 255)
YELLOW = (255, 255, 0)
RED = (255, 0, 0)
BLUE = (0, 0, 255)
GREEN = (0, 255, 0)
EXACT_PALETTE = {BLACK, WHITE, YELLOW, RED, BLUE, GREEN}


def http_get_json(url: str, timeout: int = 15):
    req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return json.loads(resp.read().decode("utf-8"))


def fetch_hn_algolia(limit: int = 10) -> list[dict]:
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


def fetch_hn_firebase(limit: int = 10) -> list[dict]:
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


def fetch_stories(limit: int = 7) -> list[dict]:
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
    date_str = now.strftime("%a, %b %d · %H:%M CST").upper()

    lead = stories[0]
    rest = stories[1:7]
    left_col = rest[:3]
    right_col = rest[3:6]

    lead_tag, lead_tag_cls = detect_tag(lead["title"])
    lead_title = html.escape(strip_prefix(lead["title"], lead_tag))
    lead_tag_html = f'<span class="pill {lead_tag_cls}">{lead_tag}</span>' if lead_tag else ""

    def render_story(rank: int, s: dict) -> str:
        tag, tag_cls = detect_tag(s["title"])
        clean_title = html.escape(strip_prefix(s["title"], tag))
        tag_html = f'<span class="mini-tag {tag_cls}">{tag}</span> ' if tag else ""
        rank_cls = "rank rank-hot" if rank <= 3 or s["points"] >= 500 else "rank"
        age = relative_age(s["created_at"], now_ts)
        return f"""
        <article class="story">
          <div class="{rank_cls}">{rank:02d}</div>
          <div class="story-body">
            <h3 class="story-title">{tag_html}{clean_title}</h3>
            <div class="story-meta">
              <span class="domain">{html.escape(s['domain'])}</span>
              <span class="dot">·</span>
              <span class="pts">▲{s['points']}</span>
              <span class="dot">·</span>
              <span class="cmts">{s['comments']}c</span>
              <span class="dot">·</span>
              <span class="age">{age}</span>
            </div>
          </div>
        </article>"""

    left_html = "\n".join(render_story(i + 2, s) for i, s in enumerate(left_col))
    right_html = "\n".join(render_story(i + 5, s) for i, s in enumerate(right_col))

    return f"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8" />
<title>Hacker News Daily — E1002 Spectra 6</title>
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
    font-family: "Helvetica Neue", Helvetica, "Liberation Sans", Arial, sans-serif;
    -webkit-font-smoothing: antialiased;
  }}

  .page {{
    width: 800px;
    height: 480px;
    padding: 12px 16px 10px 16px;
    display: flex;
    flex-direction: column;
  }}

  /* Masthead */
  .masthead {{
    display: flex;
    align-items: center;
    justify-content: space-between;
    border-bottom: 3px solid var(--ink);
    padding-bottom: 8px;
    margin-bottom: 10px;
  }}

  .brand {{
    display: flex;
    align-items: center;
    gap: 10px;
  }}

  .y-badge {{
    width: 36px;
    height: 36px;
    background: var(--red);
    color: var(--paper);
    font-weight: 900;
    font-size: 24px;
    line-height: 32px;
    text-align: center;
    border: 2px solid var(--ink);
  }}

  .masthead-title {{
    font-family: Georgia, "Times New Roman", "Liberation Serif", serif;
    font-size: 30px;
    font-weight: 900;
    letter-spacing: 0.3px;
    line-height: 1;
  }}

  .edition-meta {{
    text-align: right;
    display: flex;
    flex-direction: column;
    align-items: flex-end;
    gap: 4px;
  }}

  .edition-pill {{
    background: var(--yellow);
    color: var(--ink);
    border: 2px solid var(--ink);
    font-size: 12px;
    font-weight: 900;
    letter-spacing: 0.6px;
    padding: 1px 8px;
  }}

  .date-line {{
    font-size: 13px;
    font-weight: 800;
  }}

  /* Lead Story (#1) */
  .lead {{
    border: 2px solid var(--ink);
    border-left: 8px solid var(--red);
    padding: 9px 13px;
    margin-bottom: 10px;
    background: var(--paper);
  }}

  .lead-top {{
    display: flex;
    align-items: center;
    justify-content: space-between;
    margin-bottom: 5px;
  }}

  .lead-badges {{
    display: flex;
    align-items: center;
    gap: 8px;
  }}

  .lead-kicker {{
    background: var(--red);
    color: var(--paper);
    font-size: 12px;
    font-weight: 900;
    letter-spacing: 0.5px;
    padding: 2px 8px;
  }}

  .pill {{
    font-size: 12px;
    font-weight: 900;
    padding: 2px 7px;
    color: var(--paper);
  }}

  .lead-stats {{
    font-size: 14px;
    font-weight: 800;
    display: flex;
    gap: 10px;
    align-items: center;
  }}

  .lead-title {{
    font-family: Georgia, "Times New Roman", "Liberation Serif", serif;
    font-size: 22px;
    font-weight: 900;
    line-height: 1.2;
    display: -webkit-box;
    -webkit-line-clamp: 2;
    -webkit-box-orient: vertical;
    overflow: hidden;
  }}

  /* Two-Column Grid (3 + 3 large readable items) */
  .columns {{
    display: grid;
    grid-template-columns: 1fr 1fr;
    gap: 16px;
    flex: 1;
    min-height: 0;
  }}

  .col {{
    display: flex;
    flex-direction: column;
  }}

  .col-right {{
    border-left: 2px solid var(--ink);
    padding-left: 16px;
  }}

  .story {{
    flex: 1;
    display: flex;
    align-items: center;
    gap: 10px;
    padding: 6px 0;
    border-bottom: 1.5px solid var(--ink);
  }}

  .story:last-child {{
    border-bottom: none;
  }}

  .rank {{
    width: 30px;
    flex-shrink: 0;
    font-family: Georgia, "Times New Roman", serif;
    font-size: 21px;
    font-weight: 900;
    line-height: 1;
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
    font-size: 16.5px;
    font-weight: 800;
    line-height: 1.24;
    margin-bottom: 4px;
    display: -webkit-box;
    -webkit-line-clamp: 2;
    -webkit-box-orient: vertical;
    overflow: hidden;
  }}

  .mini-tag {{
    display: inline-block;
    font-size: 11px;
    font-weight: 900;
    padding: 1px 5px;
    vertical-align: 1px;
    color: var(--paper);
  }}

  .tag-blue {{ background: var(--blue); }}
  .tag-green {{ background: var(--green); color: var(--ink); }}
  .tag-red {{ background: var(--red); }}

  .story-meta {{
    font-size: 13px;
    font-weight: 800;
    white-space: nowrap;
    overflow: hidden;
    text-overflow: ellipsis;
  }}

  .domain {{ color: var(--blue); }}
  .pts {{ color: var(--red); font-weight: 900; }}
  .cmts {{ color: var(--ink); }}
  .age {{ color: var(--ink); }}
  .dot {{ margin: 0 4px; }}

  /* Footer */
  .footer {{
    border-top: 2px solid var(--ink);
    margin-top: 6px;
    padding-top: 5px;
    display: flex;
    justify-content: space-between;
    font-size: 12px;
    font-weight: 800;
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
          <span>{lead['comments']} cmts</span>
          <span>{relative_age(lead['created_at'], now_ts)}</span>
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
      <span><span class="footer-accent">■</span> reTerminal E1002 · Spectra 6 Crisp Mode</span>
      <span>Auto Refresh: 08:00 &amp; 20:00 CST</span>
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

    # Neutral grays (text & borders on white/black)
    if chroma < 48:
        lum = (r * 299 + g * 587 + b * 114) // 1000
        return BLACK if lum < 175 else WHITE

    # Colored pixels: check dominant channels cleanly without introducing stray hues
    # Yellow: high R & G, lower B
    if r >= 160 and g >= 160 and b <= 165:
        return YELLOW
    # Pure Blue (blue text on white or blue badge)
    if b > r + 45 and b > g + 45:
        # Light blue fringe from antialiasing against white -> keep crisp above threshold
        return WHITE if (r > 180 and g > 180) else BLUE
    # Pure Red (red text on white or red badge)
    if r > g + 45 and r > b + 45:
        return WHITE if (g > 180 and b > 180) else RED
    # Pure Green
    if g > r + 45 and g > b + 45:
        return WHITE if (r > 180 and b > 180) else GREEN

    # Fallback: nearest Euclidean distance among the 6 exact palette colors
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

    src = list(im.getdata())
    # Cache pixel mappings for speed (typically only a few hundred unique RGBs)
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
    # Non-interlaced 8-bit RGB PNG (matches check_processed_png fast path)
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
    stories = fetch_stories(limit=7)
    if len(stories) < 2:
        raise RuntimeError("Failed to fetch Hacker News stories.")
    page_html = build_html(stories, now)
    render_png(page_html, OUT_PNG)
    print(f"Rendered 1:1 Spectra 6 {OUT_PNG} ({OUT_PNG.stat().st_size} bytes) with {len(stories)} stories.")


if __name__ == "__main__":
    main()
