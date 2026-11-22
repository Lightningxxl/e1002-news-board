/**
 * Cloudflare Worker for reTerminal E1002 (esp32-photoframe).
 *
 * - GET /board.png
 *     Reads X-Temperature, X-Humidity, X-Battery-Percentage from the E1002 request
 *     (or ?temp=&hum=&bat= query params for testing), records the sample into KV
 *     (binding: SENSORS_KV), fetches the latest 800x480 1:1 Spectra 6 PNG from
 *     GitHub Pages, stamps a crisp 1:1 six-color indoor sensor badge into the
 *     image with ZERO dithering, and returns a valid non-interlaced 8-bit RGB PNG.
 *
 * - GET /sensors
 *     Returns JSON with the latest sensor reading and recent history from KV.
 *
 * - GET /
 *     Minimal mobile-friendly HTML dashboard showing latest temp/humidity/battery,
 *     history table, and live preview of /board.png.
 */

const UPSTREAM_URL = "https://lightningxxl.github.io/e1002-news-board/hn.png";
const MAX_HISTORY = 240;

// Exact Spectra 6 theoretical RGB colors (matches esp32-photoframe fast path)
const COL_BLACK = [0, 0, 0];
const COL_WHITE = [255, 255, 255];
const COL_RED = [255, 0, 0];
const COL_BLUE = [0, 0, 255];
const COL_GREEN = [0, 255, 0];
const COL_YELLOW = [255, 255, 0];

// Clean 5x7 pixel bitmap font (rendered at 2x = 10x14 px for razor-sharp e-ink legibility)
const FONT_5X7 = {
  "0": [0x0e, 0x11, 0x13, 0x15, 0x19, 0x11, 0x0e],
  "1": [0x04, 0x0c, 0x04, 0x04, 0x04, 0x04, 0x0e],
  "2": [0x0e, 0x11, 0x01, 0x06, 0x08, 0x10, 0x1f],
  "3": [0x1f, 0x02, 0x04, 0x06, 0x01, 0x11, 0x0e],
  "4": [0x02, 0x06, 0x0a, 0x12, 0x1f, 0x02, 0x02],
  "5": [0x1f, 0x10, 0x1e, 0x01, 0x01, 0x11, 0x0e],
  "6": [0x06, 0x08, 0x10, 0x1e, 0x11, 0x11, 0x0e],
  "7": [0x1f, 0x01, 0x02, 0x04, 0x08, 0x08, 0x08],
  "8": [0x0e, 0x11, 0x11, 0x0e, 0x11, 0x11, 0x0e],
  "9": [0x0e, 0x11, 0x11, 0x0f, 0x01, 0x02, 0x0c],
  "A": [0x0e, 0x11, 0x11, 0x1f, 0x11, 0x11, 0x11],
  "B": [0x1e, 0x11, 0x11, 0x1e, 0x11, 0x11, 0x1e],
  "C": [0x0e, 0x11, 0x10, 0x10, 0x10, 0x11, 0x0e],
  "E": [0x1f, 0x10, 0x10, 0x1e, 0x10, 0x10, 0x1f],
  "H": [0x11, 0x11, 0x11, 0x1f, 0x11, 0x11, 0x11],
  "I": [0x0e, 0x04, 0x04, 0x04, 0x04, 0x04, 0x0e],
  "M": [0x11, 0x1b, 0x15, 0x15, 0x11, 0x11, 0x11],
  "N": [0x11, 0x19, 0x15, 0x13, 0x11, 0x11, 0x11],
  "O": [0x0e, 0x11, 0x11, 0x11, 0x11, 0x11, 0x0e],
  "R": [0x1e, 0x11, 0x11, 0x1e, 0x14, 0x12, 0x11],
  "T": [0x1f, 0x04, 0x04, 0x04, 0x04, 0x04, 0x04],
  "U": [0x11, 0x11, 0x11, 0x11, 0x11, 0x11, 0x0e],
  ".": [0x00, 0x00, 0x00, 0x00, 0x00, 0x0c, 0x0c],
  "-": [0x00, 0x00, 0x00, 0x1f, 0x00, 0x00, 0x00],
  ":": [0x00, 0x0c, 0x0c, 0x00, 0x0c, 0x0c, 0x00],
  "%": [0x19, 0x19, 0x02, 0x04, 0x08, 0x13, 0x13],
  "°": [0x06, 0x09, 0x09, 0x06, 0x00, 0x00, 0x00],
  " ": [0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00],
  "|": [0x04, 0x04, 0x04, 0x04, 0x04, 0x04, 0x04],
};

// CRC32 table for PNG chunks
const CRC_TABLE = (() => {
  const t = new Uint32Array(256);
  for (let n = 0; n < 256; n++) {
    let c = n;
    for (let k = 0; k < 8; k++) {
      c = (c & 1) ? (0xedb88320 ^ (c >>> 1)) : (c >>> 1);
    }
    t[n] = c >>> 0;
  }
  return t;
})();

function crc32(bytes) {
  let c = 0xffffffff;
  for (let i = 0; i < bytes.length; i++) {
    c = CRC_TABLE[(c ^ bytes[i]) & 0xff] ^ (c >>> 8);
  }
  return (c ^ 0xffffffff) >>> 0;
}

function readU32BE(buf, off) {
  return (
    ((buf[off] << 24) | (buf[off + 1] << 16) | (buf[off + 2] << 8) | buf[off + 3]) >>> 0
  );
}

function writeU32BE(buf, off, val) {
  buf[off] = (val >>> 24) & 0xff;
  buf[off + 1] = (val >>> 16) & 0xff;
  buf[off + 2] = (val >>> 8) & 0xff;
  buf[off + 3] = val & 0xff;
}

async function inflateZlib(data) {
  const ds = new DecompressionStream("deflate");
  const writer = ds.writable.getWriter();
  writer.write(data);
  writer.close();
  const chunks = [];
  const reader = ds.readable.getReader();
  let total = 0;
  while (true) {
    const { done, value } = await reader.read();
    if (done) break;
    chunks.push(value);
    total += value.length;
  }
  const out = new Uint8Array(total);
  let pos = 0;
  for (const c of chunks) {
    out.set(c, pos);
    pos += c.length;
  }
  return out;
}

async function deflateZlib(data) {
  const cs = new CompressionStream("deflate");
  const writer = cs.writable.getWriter();
  writer.write(data);
  writer.close();
  const chunks = [];
  const reader = cs.readable.getReader();
  let total = 0;
  while (true) {
    const { done, value } = await reader.read();
    if (done) break;
    chunks.push(value);
    total += value.length;
  }
  const out = new Uint8Array(total);
  let pos = 0;
  for (const c of chunks) {
    out.set(c, pos);
    pos += c.length;
  }
  return out;
}

function paethPredictor(a, b, c) {
  const p = a + b - c;
  const pa = Math.abs(p - a);
  const pb = Math.abs(p - b);
  const pc = Math.abs(p - c);
  if (pa <= pb && pa <= pc) return a;
  if (pb <= pc) return b;
  return c;
}

async function decodePngRGB(pngBytes) {
  // Verify PNG signature
  if (pngBytes.length < 33 || pngBytes[0] !== 137 || pngBytes[1] !== 80) {
    throw new Error("Invalid PNG signature");
  }
  let width = 0;
  let height = 0;
  let bitDepth = 0;
  let colorType = 0;
  let palette = null;
  const idatParts = [];
  let idatTotal = 0;

  let off = 8;
  while (off + 8 <= pngBytes.length) {
    const len = readU32BE(pngBytes, off);
    const type = String.fromCharCode(
      pngBytes[off + 4],
      pngBytes[off + 5],
      pngBytes[off + 6],
      pngBytes[off + 7]
    );
    const dataStart = off + 8;
    const dataEnd = dataStart + len;
    if (type === "IHDR") {
      width = readU32BE(pngBytes, dataStart);
      height = readU32BE(pngBytes, dataStart + 4);
      bitDepth = pngBytes[dataStart + 8];
      colorType = pngBytes[dataStart + 9];
    } else if (type === "PLTE") {
      palette = pngBytes.subarray(dataStart, dataEnd);
    } else if (type === "IDAT") {
      idatParts.push(pngBytes.subarray(dataStart, dataEnd));
      idatTotal += len;
    } else if (type === "IEND") {
      break;
    }
    off = dataEnd + 4;
  }

  if (bitDepth !== 8 || (colorType !== 2 && colorType !== 6 && colorType !== 3)) {
    throw new Error(`Unsupported PNG format bitDepth=${bitDepth} colorType=${colorType}`);
  }

  const zbuf = new Uint8Array(idatTotal);
  let zoff = 0;
  for (const part of idatParts) {
    zbuf.set(part, zoff);
    zoff += part.length;
  }
  const raw = await inflateZlib(zbuf);

  const bpp = colorType === 2 ? 3 : colorType === 6 ? 4 : 1;
  const stride = width * bpp;
  const recon = new Uint8Array(height * stride);

  for (let y = 0; y < height; y++) {
    const srcRow = y * (stride + 1);
    const filter = raw[srcRow];
    const dstRow = y * stride;
    for (let x = 0; x < stride; x++) {
      const xByte = raw[srcRow + 1 + x];
      const a = x >= bpp ? recon[dstRow + x - bpp] : 0;
      const b = y > 0 ? recon[dstRow - stride + x] : 0;
      const c = x >= bpp && y > 0 ? recon[dstRow - stride + x - bpp] : 0;
      let v = xByte;
      if (filter === 1) v = (xByte + a) & 0xff;
      else if (filter === 2) v = (xByte + b) & 0xff;
      else if (filter === 3) v = (xByte + ((a + b) >> 1)) & 0xff;
      else if (filter === 4) v = (xByte + paethPredictor(a, b, c)) & 0xff;
      recon[dstRow + x] = v;
    }
  }

  // Convert to flat RGB (width * height * 3)
  if (colorType === 2) {
    return { width, height, rgb: recon };
  }
  const rgb = new Uint8Array(width * height * 3);
  if (colorType === 6) {
    for (let i = 0, j = 0; i < recon.length; i += 4, j += 3) {
      rgb[j] = recon[i];
      rgb[j + 1] = recon[i + 1];
      rgb[j + 2] = recon[i + 2];
    }
  } else if (colorType === 3 && palette) {
    for (let i = 0, j = 0; i < recon.length; i++, j += 3) {
      const idx = recon[i] * 3;
      rgb[j] = palette[idx];
      rgb[j + 1] = palette[idx + 1];
      rgb[j + 2] = palette[idx + 2];
    }
  }
  return { width, height, rgb };
}

function makeChunk(typeStr, data) {
  const out = new Uint8Array(12 + data.length);
  writeU32BE(out, 0, data.length);
  for (let i = 0; i < 4; i++) out[4 + i] = typeStr.charCodeAt(i);
  out.set(data, 8);
  const crc = crc32(out.subarray(4, 8 + data.length));
  writeU32BE(out, 8 + data.length, crc);
  return out;
}

async function encodePngRGB(width, height, rgb) {
  const stride = width * 3;
  const raw = new Uint8Array(height * (stride + 1));
  for (let y = 0; y < height; y++) {
    const dst = y * (stride + 1);
    raw[dst] = 0; // Filter None
    raw.set(rgb.subarray(y * stride, (y + 1) * stride), dst + 1);
  }
  const zdata = await deflateZlib(raw);

  const ihdr = new Uint8Array(13);
  writeU32BE(ihdr, 0, width);
  writeU32BE(ihdr, 4, height);
  ihdr[8] = 8; // bit depth
  ihdr[9] = 2; // RGB
  ihdr[10] = 0;
  ihdr[11] = 0;
  ihdr[12] = 0; // non-interlaced

  const sig = new Uint8Array([137, 80, 78, 71, 13, 10, 26, 10]);
  const cIHDR = makeChunk("IHDR", ihdr);
  const cIDAT = makeChunk("IDAT", zdata);
  const cIEND = makeChunk("IEND", new Uint8Array(0));

  const out = new Uint8Array(sig.length + cIHDR.length + cIDAT.length + cIEND.length);
  let off = 0;
  for (const part of [sig, cIHDR, cIDAT, cIEND]) {
    out.set(part, off);
    off += part.length;
  }
  return out;
}

function setPx(rgb, width, height, x, y, col) {
  if (x < 0 || y < 0 || x >= width || y >= height) return;
  const idx = (y * width + x) * 3;
  rgb[idx] = col[0];
  rgb[idx + 1] = col[1];
  rgb[idx + 2] = col[2];
}

function fillRect(rgb, width, height, x, y, w, h, col) {
  for (let dy = 0; dy < h; dy++) {
    for (let dx = 0; dx < w; dx++) {
      setPx(rgb, width, height, x + dx, y + dy, col);
    }
  }
}

function drawRectBorder(rgb, width, height, x, y, w, h, border, col) {
  fillRect(rgb, width, height, x, y, w, border, col);
  fillRect(rgb, width, height, x, y + h - border, w, border, col);
  fillRect(rgb, width, height, x, y, border, h, col);
  fillRect(rgb, width, height, x + w - border, y, border, h, col);
}

function drawText(rgb, width, height, x, y, text, scale, col) {
  let cursor = x;
  for (const ch of text) {
    const glyph = FONT_5X7[ch] || FONT_5X7[" "];
    for (let row = 0; row < 7; row++) {
      const bits = glyph[row];
      for (let c = 0; c < 5; c++) {
        if ((bits >> (4 - c)) & 1) {
          fillRect(rgb, width, height, cursor + c * scale, y + row * scale, scale, scale, col);
        }
      }
    }
    cursor += 6 * scale;
  }
  return cursor;
}

function measureText(text, scale) {
  if (!text.length) return 0;
  return text.length * 6 * scale - scale;
}

/**
 * Stamp a crisp 1:1 Spectra 6 indoor temperature & humidity badge cleanly in the
 * masthead center-right (y=7..36, between title and edition pill).
 * Battery is still recorded in KV and shown on the web dashboard, while the
 * on-device low-battery indicator handles <10% alerts automatically.
 */
function stampSensorBadge(rgb, width, height, reading) {
  if (reading.temp === null && reading.hum === null) return;

  const scale = 3; // 15x21 crisp pixels per char
  const segments = [];
  if (reading.temp !== null) {
    segments.push({ text: `${reading.temp.toFixed(1)}°C`, col: COL_RED });
  }
  if (reading.hum !== null) {
    if (segments.length) segments.push({ text: " ", col: COL_BLACK });
    segments.push({ text: `${Math.round(reading.hum)}%`, col: COL_BLUE });
  }

  const fullText = segments.map((s) => s.text).join("");
  const textW = measureText(fullText, scale);
  const padX = 8;
  const padY = 4;
  const boxW = textW + padX * 2;
  const boxH = 7 * scale + padY * 2; // 29px tall

  // Place cleanly in header open space (y=7..36, right of title, left of edition pill)
  const boxX = Math.max(280, 534 - boxW);
  const boxY = 7;

  fillRect(rgb, width, height, boxX, boxY, boxW, boxH, COL_WHITE);
  drawRectBorder(rgb, width, height, boxX, boxY, boxW, boxH, 2, COL_BLACK);

  let cx = boxX + padX;
  const cy = boxY + padY;
  for (const seg of segments) {
    cx = drawText(rgb, width, height, cx, cy, seg.text, scale, seg.col);
  }
}

function parseNum(v) {
  if (v === null || v === undefined || v === "") return null;
  const n = Number(v);
  return Number.isFinite(n) ? n : null;
}

function nowCstIso() {
  const d = new Date(Date.now() + 8 * 3600 * 1000);
  return d.toISOString().replace("T", " ").slice(0, 16) + " CST";
}

async function saveReading(env, reading) {
  if (!env || !env.SENSORS_KV) return;
  if (reading.temp === null && reading.hum === null && reading.bat === null) return;
  const record = {
    ts: Date.now(),
    time_cst: nowCstIso(),
    temp: reading.temp,
    hum: reading.hum,
    bat: reading.bat,
  };
  await env.SENSORS_KV.put("latest", JSON.stringify(record));
  const rawHist = await env.SENSORS_KV.get("history");
  let hist = [];
  if (rawHist) {
    try {
      hist = JSON.parse(rawHist);
    } catch {
      hist = [];
    }
  }
  hist.push(record);
  if (hist.length > MAX_HISTORY) {
    hist = hist.slice(hist.length - MAX_HISTORY);
  }
  await env.SENSORS_KV.put("history", JSON.stringify(hist));
}

function renderDashboardHtml(latest, history) {
  const rows = [...history]
    .reverse()
    .slice(0, 30)
    .map(
      (r) => `<tr>
        <td>${r.time_cst || ""}</td>
        <td>${r.temp !== null && r.temp !== undefined ? r.temp.toFixed(1) + " °C" : "--"}</td>
        <td>${r.hum !== null && r.hum !== undefined ? r.hum.toFixed(1) + " %" : "--"}</td>
        <td>${r.bat !== null && r.bat !== undefined ? r.bat + " %" : "--"}</td>
      </tr>`
    )
    .join("");

  const latestBanner = latest
    ? `<div class="cards">
         <div class="card"><div class="k">室内温度</div><div class="v red">${latest.temp ?? "--"} °C</div></div>
         <div class="card"><div class="k">室内湿度</div><div class="v blue">${latest.hum ?? "--"} %</div></div>
         <div class="card"><div class="k">电池电量</div><div class="v green">${latest.bat ?? "--"} %</div></div>
         <div class="card"><div class="k">最后上报</div><div class="v small">${latest.time_cst ?? "--"}</div></div>
       </div>`
    : `<p class="muted">暂无设备上报记录（按 E1002 左键拉一次图即可看到实时温湿度）。</p>`;

  return `<!DOCTYPE html>
<html lang="zh-CN">
<head>
  <meta charset="utf-8" />
  <meta name="viewport" content="width=device-width, initial-scale=1" />
  <title>E1002 温湿度 & 看板控制台</title>
  <style>
    body { font-family: -apple-system, BlinkMacSystemFont, "PingFang SC", sans-serif; background: #121212; color: #eee; max-width: 860px; margin: 0 auto; padding: 24px 16px; }
    h1 { font-size: 20px; margin-bottom: 14px; }
    .cards { display: grid; grid-template-columns: repeat(auto-fit, minmax(150px, 1fr)); gap: 12px; margin-bottom: 20px; }
    .card { background: #1e1e1e; border: 1px solid #333; border-radius: 8px; padding: 14px; }
    .k { font-size: 12px; color: #999; margin-bottom: 6px; }
    .v { font-size: 24px; font-weight: 700; }
    .v.small { font-size: 14px; margin-top: 8px; }
    .red { color: #ff6b6b; } .blue { color: #5c9eff; } .green { color: #51cf66; }
    img { width: 100%; border-radius: 6px; border: 6px solid #2b2b2b; background: #fff; margin-bottom: 20px; }
    table { width: 100%; border-collapse: collapse; font-size: 13px; background: #1a1a1a; border-radius: 8px; overflow: hidden; }
    th, td { padding: 8px 10px; border-bottom: 1px solid #2c2c2c; text-align: left; }
    th { background: #242424; color: #aaa; }
    .muted { color: #888; font-size: 13px; }
  </style>
</head>
<body>
  <h1>reTerminal E1002 · 室内温湿度 & 看板状态</h1>
  ${latestBanner}
  <img src="/board.png" alt="Live E1002 Board" />
  <h2 style="font-size:15px;margin-bottom:8px;">最近上报记录（<a href="/sensors" style="color:#5c9eff">JSON API</a>）</h2>
  <table>
    <thead><tr><th>时间</th><th>温度</th><th>湿度</th><th>电量</th></tr></thead>
    <tbody>${rows || '<tr><td colspan="4" class="muted">等待首次上报</td></tr>'}</tbody>
  </table>
</body>
</html>`;
}

export default {
  async scheduled(event, env, ctx) {
    if (!env.GH_DISPATCH_TOKEN) {
      console.error("GH_DISPATCH_TOKEN secret is not configured");
      return;
    }
    const modeByCron = {
      "45 23 * * *": "morning",
      "45 4 * * *": "midday",
      "45 11 * * *": "evening",
    };
    const mode = modeByCron[event.cron] || "auto";
    const resp = await fetch(
      "https://api.github.com/repos/Lightningxxl/e1002-news-board/actions/workflows/update.yml/dispatches",
      {
        method: "POST",
        headers: {
          Authorization: `Bearer ${env.GH_DISPATCH_TOKEN}`,
          Accept: "application/vnd.github+json",
          "User-Agent": "e1002-board-worker/1.0",
          "Content-Type": "application/json",
        },
        body: JSON.stringify({ ref: "main", inputs: { mode } }),
      }
    );
    if (!resp.ok) {
      console.error("GitHub workflow dispatch failed:", resp.status, await resp.text());
    } else {
      console.log(`Dispatched update.yml (mode=${mode}) for cron "${event.cron}"`);
    }
  },

  async fetch(request, env, ctx) {
    const url = new URL(request.url);

    if (url.pathname === "/sensors") {
      const latestRaw = env.SENSORS_KV ? await env.SENSORS_KV.get("latest") : null;
      const histRaw = env.SENSORS_KV ? await env.SENSORS_KV.get("history") : null;
      return new Response(
        JSON.stringify(
          {
            latest: latestRaw ? JSON.parse(latestRaw) : null,
            history: histRaw ? JSON.parse(histRaw) : [],
          },
          null,
          2
        ),
        { headers: { "Content-Type": "application/json; charset=utf-8", "Cache-Control": "no-store" } }
      );
    }

    if (url.pathname === "/" || url.pathname === "/index.html") {
      const latestRaw = env.SENSORS_KV ? await env.SENSORS_KV.get("latest") : null;
      const histRaw = env.SENSORS_KV ? await env.SENSORS_KV.get("history") : null;
      const latest = latestRaw ? JSON.parse(latestRaw) : null;
      const history = histRaw ? JSON.parse(histRaw) : [];
      return new Response(renderDashboardHtml(latest, history), {
        headers: { "Content-Type": "text/html; charset=utf-8", "Cache-Control": "no-store" },
      });
    }

    if (url.pathname === "/board.png" || url.pathname === "/hn.png") {
      const hdrTemp = parseNum(request.headers.get("X-Temperature"));
      const hdrHum = parseNum(request.headers.get("X-Humidity"));
      const hdrBat = parseNum(request.headers.get("X-Battery-Percentage"));

      const qryTemp = parseNum(url.searchParams.get("temp"));
      const qryHum = parseNum(url.searchParams.get("hum"));
      const qryBat = parseNum(url.searchParams.get("bat"));

      const isFromDevice = hdrTemp !== null || hdrHum !== null || hdrBat !== null;

      let reading = {
        temp: hdrTemp ?? qryTemp,
        hum: hdrHum ?? qryHum,
        bat: hdrBat ?? qryBat,
      };

      // Persist real device reports asynchronously
      if (isFromDevice && env.SENSORS_KV) {
        ctx.waitUntil(saveReading(env, reading));
      } else if (
        reading.temp === null &&
        reading.hum === null &&
        reading.bat === null &&
        env.SENSORS_KV
      ) {
        // Browser preview fallback: show last recorded device sensor reading
        const latestRaw = await env.SENSORS_KV.get("latest");
        if (latestRaw) {
          const latest = JSON.parse(latestRaw);
          reading = { temp: latest.temp ?? null, hum: latest.hum ?? null, bat: latest.bat ?? null };
        }
      }

      const upstreamResp = await fetch(UPSTREAM_URL, {
        cf: { cacheTtl: 60, cacheEverything: true },
      });
      if (!upstreamResp.ok) {
        return new Response(`Upstream error: ${upstreamResp.status}`, { status: 502 });
      }

      const upstreamEtag = upstreamResp.headers.get("ETag") || "";
      const sensorKey = `v2-${reading.temp ?? ""}_${reading.hum ?? ""}`;
      const combinedEtag = `"${upstreamEtag.replace(/"/g, "")}-${sensorKey}"`;

      const ifNoneMatch = request.headers.get("If-None-Match");
      if (ifNoneMatch && ifNoneMatch === combinedEtag) {
        return new Response(null, {
          status: 304,
          headers: { ETag: combinedEtag, "Cache-Control": "no-store" },
        });
      }

      const pngBytes = new Uint8Array(await upstreamResp.arrayBuffer());
      let outBytes = pngBytes;

      if (reading.temp !== null || reading.hum !== null || reading.bat !== null) {
        const { width, height, rgb } = await decodePngRGB(pngBytes);
        stampSensorBadge(rgb, width, height, reading);
        outBytes = await encodePngRGB(width, height, rgb);
      }

      return new Response(outBytes, {
        status: 200,
        headers: {
          "Content-Type": "image/png",
          ETag: combinedEtag,
          "Cache-Control": "no-store",
        },
      });
    }

    return new Response("Not Found", { status: 404 });
  },
};
