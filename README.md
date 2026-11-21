# E1002 News & Markets Board

Automated `800×480` 1:1 pixel-exact **E Ink Spectra 6** daily newspaper generator for **Seeed Studio reTerminal E1002** running [`aitjcize/esp32-photoframe`](https://github.com/aitjcize/esp32-photoframe).

## Coverage

- **US Markets (Intraday + Pre/After-Hours)**: S&P 500 (`SPY`), Nasdaq 100 (`QQQ`), Semiconductors (`SOXX`), Consumer Discretionary (`XLY`)
- **CN Markets (Intraday + After-Hours/Level)**: CSI 300 (`沪深300`), China Tech (`KWEB` with AH), A-Share Semiconductors (`512480`), A-Share Consumer (`159928`)
- **US Tech & Finance News**: Hacker News + CNBC Tech/Finance
- **CN Finance & Tech News**: WallstreetCN (华尔街见闻)

## Live Image URL

```text
https://lightningxxl.github.io/e1002-news-board/hn.png
```

## Local Run

```bash
python3 scripts/generate_board.py
```
