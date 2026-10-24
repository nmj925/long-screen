# Long Screen

A long-hold stock screener with its own dashboard, covering about 8,000 stocks worldwide. It runs free on GitHub: prices refresh every hour on weekdays, and fundamentals refresh every weekday after the US close. You don't need TradingView Premium or a paid data feed.

- **Universe:**
  - **US:** the S&P 500 + 400 + 600, about 1,500 stocks.
  - **International:** every stock worth **$2B or more** on the main exchange of about 50 countries. That includes Canada, the UK, the main European markets, Japan, China, Hong Kong, India, Korea, Taiwan, Australia, Southeast Asia, Latin America, the Middle East and South Africa.
  - **Duplicate listings removed.** Shell in London and Amsterdam, or US stocks listed on Mexico's exchange, appear only once.
- **Currencies:** prices are shown in each stock's local currency. Market caps and cash-flow figures are converted to US dollars, so the tests compare like with like.
- **Data:** Yahoo Finance via `yfinance`, using TTM figures plus four years of annual statements.
- **Dashboard:** a static web page hosted free on GitHub Pages. It works on a phone too.

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
- **Balance sheet:** net debt/EBITDA, interest coverage, current ratio, share count change
- **Quality scores:** Piotroski F-score (0–9), Altman Z-score
- **Momentum and risk:** 6-month return, price vs 200-day average, distance from 52-week high, volatility, 1Y max drawdown, beta
- **Street and ownership:** analyst target upside, analyst rating, insider ownership, short interest

The dashboard also has **Markets** and **Sectors** filters, so you can look at Japan only, or everything except China, for example.

Every stock gets a **composite score** from 0 to 100: quality 35%, growth 25%, value 25% and momentum 15%. Each part is a percentile rank against the eligible universe.

### How the main numbers are computed

- **ROIC** = EBIT × (1 − effective tax rate) ÷ (total debt + equity − cash).
- **3Y CAGRs** compare the latest fiscal year with the one three years earlier. If either end is negative, the CAGR is left blank.
- **12-1 momentum** is the return from 12 months ago to 1 month ago.
- **Missing data counts as a fail.** The *Near misses* tab shows stocks that fail exactly one test.
- **Currency checks:** Yahoo isn't consistent about currencies. It mixes pence and pounds on London stocks, and some ADRs are priced in dollars but report in other currencies. Where Yahoo gives its own P/E or P/S as a cross-check, the screener uses it to pick the right conversion.
- **Hourly updates** recompute everything that depends on price. Fundamentals only change on the daily run.

## Setup

1. **Create a public repository** on github.com. It must be public for free GitHub Pages.
2. **Upload these files.** GitHub's upload page won't take the hidden `.github` folder, so create the two workflow files with *Add file → Create new file*, naming them `.github/workflows/full-screen.yml` and `.github/workflows/prices.yml`, and paste in their contents.
3. **Allow the workflows to write:** *Settings → Actions → General → Workflow permissions →* **Read and write permissions** → Save.
4. **Run it once:** *Actions → Full screen (daily) → Run workflow.*
5. **Point the website at the results:** when the run finishes, a branch called `gh-pages` will exist. Go to *Settings → Pages → Build and deployment →* Source **Deploy from a branch**, Branch **gh-pages**, folder **/ (root)** → Save.
6. Open `https://<you>.github.io/<repo>/`.

### How long runs take

| Run | Time |
|---|---|
| First full run | 3–4.5 hours. Prices and TTM data load for every stock, plus statements for the 2,500 biggest companies. |
| Next 2–3 daily runs | 2–4 hours each, filling in statements for the rest of the universe. |
| Normal daily run after that | about 1–2 hours |
| Hourly price refresh | about 2–5 minutes |

Until a company's statements have been pulled, metrics that need them (3Y growth, ROIC, F-score and so on) show "no data", so the screen fills out over the first few days.

**Why results go to a `gh-pages` branch:** with about 8,000 stocks updating hourly, saving each update on `main` would bloat the repository by gigabytes a year. The `gh-pages` branch is overwritten each time, so it stays small. Your code stays on `main`.

### Schedule

| Workflow | When (UTC) | What it does |
|---|---|---|
| Price refresh (hourly) | :35 past every hour, Mon–Fri | Latest prices and all price-based ratios, covering Asian, European and US sessions |
| Full screen (daily) | 21:45, Mon–Fri | Universe, TTM data, statements, scores, history |

## Running it on your own computer

```bash
pip install -r requirements.txt
python screener.py full              # several hours the first time
python screener.py prices
python -m http.server -d docs 8000   # then open http://localhost:8000
```

- `python screener.py full --limit 100` runs a quick trial on 100 tickers.
- `python tools/make_demo.py` regenerates the sample data.
- `python -m pytest tests` runs the offline tests.

## Customising (top of `screener.py`)

- **`CRITERIA`:** default thresholds and which tests are on.
- **`INTERNATIONAL = False`:** go back to US only.
- **`INTL_MIN_MCAP_USD`:** the size floor for international stocks. Lowering it adds many more stocks and makes runs much slower.
- **`INTL_EXCHANGES`:** delete a country's line to leave it out.
- **`EXTRA_TICKERS`:** add anything else, using Yahoo symbols such as `"7203.T"` or `"MC.PA"`.
- **`STATEMENT_BUDGET`, `TIME_BUDGET_MIN`, `WORKERS`:** how hard each run works.

## Things to know

- Yahoo's data is free and generally good, but it isn't audited. International coverage is thinner than US coverage, especially for smaller markets. Check anything you plan to act on against the company's annual report.
- Accounting standards differ between countries (US GAAP vs IFRS), so the same metric can be measured a little differently.
- Hourly prices for markets that are closed simply repeat the last close.
- If Yahoo rate-limits a run, it retries with backoff. If fewer than half the stocks load, the workflow is marked failed and GitHub emails you, but the data that did load is still published. Lower `WORKERS` if this keeps happening.
- GitHub may pause scheduled workflows after 60 days with no activity in the repository. Re-enable them from the Actions tab if that happens.
- This is a screen that produces candidates, not a buy list.
