# E1002 News Board

Automated `800×480` E Ink Spectra 6 newspaper image generator for **Seeed Studio reTerminal E1002** running [`aitjcize/esp32-photoframe`](https://github.com/aitjcize/esp32-photoframe).

## Live Image URL

```text
https://lightningxxl.github.io/e1002-news-board/hn.png
```

## Recommended Device Settings (`esp32-photoframe`)

- **Rotation Mode**: `URL`
- **Image URL**: `https://lightningxxl.github.io/e1002-news-board/hn.png`
- **Timezone**: `CST-8` (`Asia/Shanghai`)
- **Cron Schedule**: `0 8 *` and `0 20 *` (08:00 and 20:00 CST daily)
- **Deep Sleep**: Enabled

## Local Run

```bash
python3 scripts/generate_hn.py
```
