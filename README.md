# Long Screen

A long-hold stock screener with its own dashboard. It runs free on GitHub: prices refresh hourly during US market hours, and fundamentals refresh every weekday after the close. You don't need TradingView Premium or a paid data feed.

- **Universe:** S&P 500 + 400 + 600 (about 1,500 US stocks), with room to add your own tickers.
- **Data:** Yahoo Finance via `yfinance`, using TTM figures plus four years of annual statements.
- **Dashboard:** a static web page (`docs/`) hosted free on GitHub Pages. It works on a phone too.

## What it screens

The **default screen** is the agreed quality-at-a-reasonable-price core:

| Test | Default |
|---|---|
| Market cap | ≥ $2B |
| Valuation | P/E 0–30, **or** PEG 0–2 for faster growers |
| Revenue growth, 3Y CAGR | ≥ 10% |
| ROIC, latest fiscal year | ≥ 15% |
| Gross margin | ≥ 40% |
| FCF margin | ≥ 10% |
| Debt / equity | ≤ 1.0 |
| 12-1 month momentum | ≥ 0% |
| Sectors | Financials and Real Estate excluded |

There are **28 more metrics** you can switch on with one click in the dashboard:

- **Valuation:** forward P/E, EV/EBITDA, FCF yield, price/sales
- **Growth:** EPS 3Y CAGR, years of revenue growth, latest-quarter revenue growth
- **Profitability:** ROIC multi-year average, ROE, worst-year gross margin, operating margin, years of positive FCF
- **Balance sheet:** net debt/EBITDA, interest coverage, current ratio, share count change (dilution or buybacks)
- **Quality scores:** Piotroski F-score (0–9), Altman Z-score
- **Momentum and risk:** 6-month return, price vs 200-day average, distance from 52-week high, volatility, 1Y max drawdown, beta
- **Street and ownership:** analyst target upside, analyst rating, insider ownership, short interest

Every stock also gets a **composite score** from 0 to 100: quality 35%, growth 25%, value 25% and momentum 15%. Each part is a percentile rank against the eligible universe. By default, the Passing table is sorted by this score.

### How the main numbers are computed

- **ROIC** = EBIT × (1 − effective tax rate) ÷ (total debt + equity − cash). The tax rate comes from the same year and is clamped to 0–35%, with 21% used if it's missing. Unlike ROE, debt can't inflate it.
- **3Y CAGRs** compare the latest fiscal year with the one three years earlier. If either end is negative, the CAGR is left blank, because a growth rate from a loss means nothing.
- **12-1 momentum** is the return from 12 months ago to 1 month ago. It skips the latest month, which tends to reverse.
- **Missing data counts as a fail.** In the table it's grey and marked "no data". The *Near misses* tab shows stocks that fail exactly one test, which is where a one-off distortion usually shows up.
- The hourly job recomputes all price-dependent figures from the stored base values: P/E, PEG, forward P/E, market cap, FCF yield, P/S, EV/EBITDA, analyst upside, returns, 52-week high distance, 200-day trend and the Z-score. Fundamentals only change on the daily run.

## Setup (about 10 minutes)

1. **Create a repository** on github.com, for example `long-screen`. It must be **Public** to get free GitHub Pages hosting. Nothing private is stored in it.
2. **Upload this folder.** Use *Add file → Upload files* and drag in everything, including the hidden `.github` folder. Or use git:
   ```bash
   git init && git add . && git commit -m "Long Screen"
   git branch -M main
   git remote add origin https://github.com/<you>/long-screen.git
   git push -u origin main
   ```
3. **Allow the workflows to save results:** *Settings → Actions → General → Workflow permissions →* **Read and write permissions** → Save.
4. **Turn on the website:** *Settings → Pages → Build and deployment →* Source **Deploy from a branch**, Branch **main**, folder **/docs** → Save.
5. **Run it once now:** *Actions → Full screen (daily) → Run workflow.* The first run pulls four years of statements for about 1,200 companies, so it takes roughly 30–60 minutes. Later runs only re-pull statements older than a week, so they're faster.
6. Open `https://<you>.github.io/long-screen/`. Until step 5 finishes, it shows the sample data with a banner.

After that, everything is automatic:

| Workflow | When (UTC) | What it does |
|---|---|---|
| Price refresh (hourly) | :35 past each hour, 13:35–20:35, Mon–Fri | Latest prices and all price-based ratios |
| Full screen (daily) | 21:45, Mon–Fri | Universe, TTM data, statements, scores, history |

An open dashboard tab checks for new data every 10 minutes.

### Running it on your own computer instead

```bash
pip install -r requirements.txt
python screener.py full          # about 30-60 min the first time
python screener.py prices        # quick price refresh
python -m http.server -d docs 8000   # then open http://localhost:8000
```

Use `python screener.py full --limit 50` for a quick trial run. `python tools/make_demo.py` regenerates the sample data, and `python -m pytest tests` runs the offline tests.

## Customising

- **Default thresholds and which tests are on:** edit the `CRITERIA` list at the top of `screener.py`. The dashboard reads its defaults from there.
- **Your own changes in the dashboard** are saved in your browser. *Reset to defaults* puts them back.
- **Extra tickers** such as ADRs or anything outside the S&P 1500: add them to `EXTRA_TICKERS`.
- **Sectors:** click the chips in the dashboard, or edit `EXCLUDE_SECTORS` to change the default.
- **Schedule:** edit the `cron` lines in `.github/workflows/`.

## Things to know

- Yahoo's data is free and generally good, but it isn't audited. Check anything you plan to act on against the 10-K. Each stock's detail panel links to its filings.
- If Yahoo rate-limits a run, the job retries with backoff. If fewer than half the tickers load, the workflow is marked failed and GitHub emails you, but the data that did load is still saved. Lower `WORKERS` in `screener.py` if this keeps happening.
- GitHub may pause scheduled workflows in a repository that has had no activity for 60 days. If that happens, re-enable them from the Actions tab.
- The new-entries, dropped and history views use the **default** criteria, so the record stays comparable from day to day.
- This is a screen that produces candidates, not a buy list.
