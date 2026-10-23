#!/usr/bin/env python3
"""Long-hold stock screener: quality at a reasonable price, S&P 1500 universe.

Two modes:
  python screener.py full     # fundamentals + prices for the whole universe (daily, after the close)
  python screener.py prices   # refresh prices and price-based ratios only (hourly, during market hours)

Writes the dashboard's data files to docs/data/. Data comes from Yahoo Finance via yfinance (free).
Edit CRITERIA below to change the default screen; the dashboard reads its defaults from here.
"""
from __future__ import annotations

import argparse
import datetime as dt
import io
import json
import math
import sys
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

import numpy as np
import pandas as pd
import requests

ROOT = Path(__file__).resolve().parent
DATA = ROOT / "docs" / "data"
CACHE = ROOT / "cache"

# ---------------------------------------------------------------------------
# CONFIG
# ---------------------------------------------------------------------------
EXCLUDE_SECTORS = ["Financial Services", "Real Estate"]  # D/E, ROE and FCF mean different things there
EXTRA_TICKERS: list[str] = []          # add anything outside the S&P 1500, e.g. ["ASML", "TSM"]
STATEMENT_MAX_AGE_DAYS = 7             # annual statements are re-pulled at most weekly
STATEMENT_MIN_MCAP = 1e9               # skip statement pulls for tiny names (saves requests)
WORKERS = 6                            # parallel Yahoo requests; lower it if you get rate limited
HISTORY_DAYS = 400

# Each criterion: key, group, label, op (min/max/pe_or_peg), default value, on by default, format, help.
# The ones switched on are the agreed core screen; the rest are one click away in the dashboard.
CRITERIA = [
    # Size & valuation
    ("mcap", "Size & valuation", "Market cap", "min", 2e9, True, "usd", "Liquidity and survivorship floor."),
    ("pe_or_peg", "Size & valuation", "P/E or PEG", "pe_or_peg", [30, 2.0], True, "x",
     "Passes if trailing P/E is 0-30, or PEG is 0-2 for faster growers."),
    ("fpe", "Size & valuation", "Forward P/E", "max", 25, False, "x", "Price over next-12-month consensus EPS."),
    ("ev_ebitda", "Size & valuation", "EV / EBITDA", "max", 18, False, "x", "Capital-structure-neutral valuation."),
    ("fcf_yield", "Size & valuation", "FCF yield", "min", 0.03, False, "pct", "Trailing free cash flow / market cap."),
    ("ps", "Size & valuation", "Price / sales", "max", 8, False, "x", "Useful for low-margin or cyclical names."),
    # Growth
    ("rev3y", "Growth", "Revenue growth, 3Y CAGR", "min", 0.10, True, "pct", "From annual income statements."),
    ("rev_up", "Growth", "Years revenue grew (of last 3)", "min", 3, False, "int", "Consistency of the top line."),
    ("eps3y", "Growth", "EPS growth, 3Y CAGR", "min", 0.10, False, "pct", "Diluted EPS; needs positive EPS at both ends."),
    ("rev_q", "Growth", "Revenue growth, latest quarter YoY", "min", 0.05, False, "pct", "Is growth still there right now?"),
    # Profitability
    ("roic", "Profitability", "ROIC, latest year", "min", 0.15, True, "pct",
     "NOPAT / (debt + equity - cash). Not inflated by leverage like ROE."),
    ("roic_avg", "Profitability", "ROIC, multi-year average", "min", 0.15, False, "pct", "Average of up to 4 fiscal years."),
    ("roe", "Profitability", "Return on equity", "min", 0.15, False, "pct", "Trailing. Read alongside debt/equity."),
    ("gm", "Profitability", "Gross margin", "min", 0.40, True, "pct", "Rough moat / pricing-power proxy."),
    ("gm_min", "Profitability", "Gross margin, worst year", "min", 0.35, False, "pct", "Margin durability through the cycle."),
    ("om", "Profitability", "Operating margin", "min", 0.15, False, "pct", "Trailing twelve months."),
    ("fcfm", "Profitability", "FCF margin", "min", 0.10, True, "pct", "Trailing free cash flow / revenue."),
    ("fcf_years", "Profitability", "Years of positive FCF (of last 4)", "min", 4, False, "int", "Cash generation consistency."),
    # Balance sheet
    ("de", "Balance sheet", "Debt / equity", "max", 1.0, True, "x", "Negative equity counts as a fail."),
    ("nd_ebitda", "Balance sheet", "Net debt / EBITDA", "max", 2.0, False, "x", "Negative means net cash."),
    ("int_cov", "Balance sheet", "Interest coverage", "min", 8, False, "x", "EBIT / interest expense."),
    ("cur_ratio", "Balance sheet", "Current ratio", "min", 1.0, False, "x", "Short-term liquidity."),
    ("dilution", "Balance sheet", "Share count change per year", "max", 0.02, False, "pct", "Negative = buybacks."),
    # Quality scores
    ("fscore", "Quality scores", "Piotroski F-score", "min", 6, False, "int", "0-9 checklist of improving fundamentals."),
    ("zscore", "Quality scores", "Altman Z-score", "min", 3.0, False, "num", "Above 3 is the classic safe zone."),
    # Momentum & risk
    ("mom", "Momentum & risk", "12-1 month momentum", "min", 0.0, True, "pct",
     "Return from 12 months ago to 1 month ago. The momentum signal that persists."),
    ("perf6m", "Momentum & risk", "6-month return", "min", 0.0, False, "pct", ""),
    ("dma200", "Momentum & risk", "Price vs 200-day average", "min", 0.0, False, "pct", "Long-term trend filter."),
    ("off_high", "Momentum & risk", "Distance from 52-week high", "min", -0.25, False, "pct", "-25% means within 25% of the high."),
    ("vol", "Momentum & risk", "Volatility, 1Y annualised", "max", 0.40, False, "pct", ""),
    ("mdd", "Momentum & risk", "Max drawdown, 1Y", "min", -0.35, False, "pct", ""),
    ("beta", "Momentum & risk", "Beta", "max", 1.5, False, "num", "Sensitivity to the market."),
    # Street & ownership
    ("upside", "Street & ownership", "Analyst target upside", "min", 0.0, False, "pct", "Mean target vs price."),
    ("rec", "Street & ownership", "Analyst rating (1 buy, 5 sell)", "max", 2.5, False, "num", ""),
    ("insider", "Street & ownership", "Insider ownership", "min", 0.01, False, "pct", "Skin in the game."),
    ("short", "Street & ownership", "Short interest, % of float", "max", 0.10, False, "pct", ""),
]

# Display-only fields (shown in the table / detail panel, not filterable)
DISPLAY = {
    "price": ("Price", "usd2"), "chg1d": ("1-day change", "pct"), "pe": ("P/E", "x"), "peg": ("PEG", "x"),
    "perf3m": ("3-month return", "pct"), "perf12m": ("12-month return", "pct"), "divy": ("Dividend yield", "pct"),
    "inst": ("Institutional ownership", "pct"), "analysts": ("Analysts covering", "int"),
    "score": ("Composite score", "int"), "coverage": ("Data coverage", "pct"),
}

# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------

def now_utc() -> str:
    return dt.datetime.now(dt.timezone.utc).replace(microsecond=0).isoformat()


def num(x):
    """Coerce to a finite float or None."""
    try:
        f = float(x)
    except (TypeError, ValueError):
        return None
    return f if math.isfinite(f) else None


def ratio(a, b):
    a, b = num(a), num(b)
    if a is None or b is None or b == 0:
        return None
    return a / b


def rnd(x, d=4):
    x = num(x)
    return None if x is None else round(x, d)


def sig(x, digits=6):
    """Round to significant digits (for large base values like revenue)."""
    x = num(x)
    if x is None or x == 0:
        return x
    return round(x, digits - 1 - int(math.floor(math.log10(abs(x)))))


def load_json(path: Path, default):
    try:
        return json.loads(path.read_text())
    except (OSError, ValueError):
        return default


def save_json(path: Path, obj, records=False):
    """Write JSON. records=True puts one record per line so git diffs stay small."""
    path.parent.mkdir(parents=True, exist_ok=True)
    if records and isinstance(obj, dict) and "stocks" in obj:
        head = {k: v for k, v in obj.items() if k != "stocks"}
        lines = [json.dumps(s, separators=(",", ":")) for s in obj["stocks"]]
        text = json.dumps(head, separators=(",", ":"))[:-1] + ',"stocks":[\n' + ",\n".join(lines) + "\n]}\n"
    else:
        text = json.dumps(obj, separators=(",", ":"))
    tmp = path.with_suffix(".tmp")
    tmp.write_text(text)
    tmp.replace(path)


def with_retries(fn, tries=4, base=3.0):
    for i in range(tries):
        try:
            return fn()
        except Exception as e:  # yfinance raises a mix of types; back off harder on rate limits
            if i == tries - 1:
                raise
            limited = "RateLimit" in type(e).__name__ or "429" in str(e) or "Too Many" in str(e)
            time.sleep(base * (2 ** i) * (4 if limited else 1))


# ---------------------------------------------------------------------------
# universe
# ---------------------------------------------------------------------------
WIKI = {
    "S&P 500": "https://en.wikipedia.org/wiki/List_of_S%26P_500_companies",
    "S&P 400": "https://en.wikipedia.org/wiki/List_of_S%26P_400_companies",
    "S&P 600": "https://en.wikipedia.org/wiki/List_of_S%26P_600_companies",
}


def get_universe() -> dict[str, str]:
    """{ticker: index name}. Falls back to the last good copy if Wikipedia is unreachable."""
    cache_file = CACHE / "universe.json"
    out: dict[str, str] = {}
    headers = {"User-Agent": "Mozilla/5.0 (long-screener; personal use)"}
    for idx, url in WIKI.items():
        try:
            html = requests.get(url, headers=headers, timeout=30).text
            for table in pd.read_html(io.StringIO(html)):
                col = next((c for c in table.columns if str(c).lower() in ("symbol", "ticker", "ticker symbol")), None)
                if col is not None:
                    for t in table[col].dropna().astype(str):
                        out.setdefault(t.strip().replace(".", "-"), idx)
                    break
        except Exception as e:
            print(f"  ! could not load {idx} list: {e}", file=sys.stderr)
    for t in EXTRA_TICKERS:
        out.setdefault(t, "Extra")
    if len(out) > 1000:
        save_json(cache_file, out)
        return out
    cached = load_json(cache_file, {})
    if cached:
        print(f"  using cached universe ({len(cached)} tickers)")
        cached.update({t: "Extra" for t in EXTRA_TICKERS if t not in cached})
        return cached
    if out:
        return out
    raise SystemExit("No universe available: Wikipedia unreachable and no cache/universe.json yet.")


# ---------------------------------------------------------------------------
# statements -> multi-year metrics (pure functions, unit-tested)
# ---------------------------------------------------------------------------

def _row(df: pd.DataFrame | None, *names) -> pd.Series | None:
    if df is None or df.empty:
        return None
    for n in names:
        if n in df.index:
            return pd.to_numeric(df.loc[n], errors="coerce")
    return None


def _sorted(df):
    if df is None or getattr(df, "empty", True):
        return None
    df = df.copy()
    df.columns = pd.to_datetime(df.columns, errors="coerce")
    df = df.loc[:, df.columns.notna()]
    return df.sort_index(axis=1, ascending=False)  # newest first


def _cagr(series: pd.Series | None, min_years=2, max_years=3):
    """CAGR between the newest value and the value up to max_years earlier. None if signs or data don't allow it."""
    if series is None:
        return None
    s = series.dropna()
    if len(s) < min_years + 1:
        return None
    s = s.iloc[: max_years + 1]
    first, last = s.iloc[-1], s.iloc[0]
    years = (s.index[0] - s.index[-1]).days / 365.25
    if first <= 0 or last <= 0 or years < min_years - 0.25:
        return None
    return (last / first) ** (1 / years) - 1


def _at(series, i):
    if series is None or i >= len(series):
        return None
    return num(series.iloc[i])


def statement_metrics(inc: pd.DataFrame | None, bal: pd.DataFrame | None, cf: pd.DataFrame | None) -> dict:
    inc, bal, cf = _sorted(inc), _sorted(bal), _sorted(cf)
    m: dict = {}

    rev = _row(inc, "Total Revenue", "Operating Revenue")
    gp = _row(inc, "Gross Profit")
    ebit = _row(inc, "EBIT", "Operating Income")
    tax = _row(inc, "Tax Provision")
    pretax = _row(inc, "Pretax Income")
    ni = _row(inc, "Net Income", "Net Income Common Stockholders")
    eps = _row(inc, "Diluted EPS", "Basic EPS")
    shares = _row(inc, "Diluted Average Shares", "Basic Average Shares")
    intexp = _row(inc, "Interest Expense", "Interest Expense Non Operating")

    debt = _row(bal, "Total Debt")
    equity = _row(bal, "Stockholders Equity", "Common Stock Equity")
    cash = _row(bal, "Cash Cash Equivalents And Short Term Investments", "Cash And Cash Equivalents")
    assets = _row(bal, "Total Assets")
    cur_a = _row(bal, "Current Assets")
    cur_l = _row(bal, "Current Liabilities")
    ltd = _row(bal, "Long Term Debt")
    liab = _row(bal, "Total Liabilities Net Minority Interest")
    re = _row(bal, "Retained Earnings")
    shares_out = _row(bal, "Ordinary Shares Number", "Share Issued")

    ocf = _row(cf, "Operating Cash Flow")
    fcf = _row(cf, "Free Cash Flow")
    if fcf is None and ocf is not None:
        capex = _row(cf, "Capital Expenditure")
        fcf = ocf + capex if capex is not None else None

    # growth
    m["rev3y"] = _cagr(rev)
    m["eps3y"] = _cagr(eps)
    if rev is not None:
        r = rev.dropna().iloc[:4]
        m["rev_up"] = int(sum(r.iloc[i] > r.iloc[i + 1] for i in range(len(r) - 1))) if len(r) >= 2 else None
        m["revh"] = [[d.year, round(float(v) / 1e6)] for d, v in r.iloc[::-1].items()]

    # margins over time
    if rev is not None and gp is not None:
        gms = (gp / rev).dropna()
        gms = gms[np.isfinite(gms)]
        m["gm_min"] = num(gms.iloc[:4].min()) if len(gms) else None

    # ROIC by year
    roics = []
    n_years = len(inc.columns) if inc is not None else 0
    for i in range(min(n_years, 4)):
        e = _at(ebit, i)
        if e is None or bal is None:
            continue
        d_inc = inc.columns[i]
        # match the balance sheet column with the same fiscal year end (within ~45 days)
        j = next((k for k, c in enumerate(bal.columns) if abs((c - d_inc).days) < 45), None)
        if j is None:
            continue
        t_rate = ratio(_at(tax, i), _at(pretax, i))
        t_rate = 0.21 if t_rate is None else min(max(t_rate, 0.0), 0.35)
        invested = (_at(debt, j) or 0) + (_at(equity, j) or 0) - (_at(cash, j) or 0)
        if _at(equity, j) is None or invested <= 0:
            continue
        roics.append(e * (1 - t_rate) / invested)
    m["roic"] = roics[0] if roics else None
    m["roic_avg"] = float(np.mean(roics)) if len(roics) >= 2 else None

    # FCF consistency
    if fcf is not None:
        f = fcf.dropna().iloc[:4]
        m["fcf_years"] = int((f > 0).sum()) if len(f) else None

    # interest coverage
    e0, i0 = _at(ebit, 0), _at(intexp, 0)
    if e0 is not None and i0 not in (None, 0):
        m["int_cov"] = e0 / abs(i0)
    elif e0 is not None and e0 > 0 and (_at(debt, 0) or 0) <= 0.01 * (_at(assets, 0) or 1):
        m["int_cov"] = 999.0  # effectively debt-free

    # dilution (annualised change in share count)
    sh = shares if shares is not None and shares.dropna().size >= 2 else shares_out
    if sh is not None:
        s = sh.dropna().iloc[:4]
        if len(s) >= 2 and s.iloc[-1] > 0:
            years = max((s.index[0] - s.index[-1]).days / 365.25, 0.5)
            m["dilution"] = (s.iloc[0] / s.iloc[-1]) ** (1 / years) - 1

    # Piotroski F-score (needs two fiscal years)
    def g(series, i):
        return _at(series, i)

    def gt(a, b):
        return a is not None and b is not None and a > b

    if all(x is not None for x in (g(ni, 0), g(ni, 1), g(assets, 0), g(assets, 1))) and g(assets, 0) and g(assets, 1):
        roa0, roa1 = ratio(g(ni, 0), g(assets, 0)), ratio(g(ni, 1), g(assets, 1))
        cfo0 = g(ocf, 0)
        lev0, lev1 = ratio(g(ltd, 0) or 0, g(assets, 0)), ratio(g(ltd, 1) or 0, g(assets, 1))
        s0 = g(sh, 0) if sh is not None else None
        s1 = g(sh, 1) if sh is not None else None
        pts = sum([
            gt(roa0, 0),                                                        # profitable
            gt(cfo0, 0),                                                        # cash generative
            gt(roa0, roa1),                                                     # ROA improving
            gt(cfo0, g(ni, 0)),                                                 # earnings backed by cash
            not gt(lev0, lev1),                                                 # leverage not rising
            gt(ratio(g(cur_a, 0), g(cur_l, 0)), ratio(g(cur_a, 1), g(cur_l, 1))),  # liquidity improving
            s0 is not None and s1 is not None and s0 <= s1 * 1.005,             # no dilution
            gt(ratio(g(gp, 0), g(rev, 0)), ratio(g(gp, 1), g(rev, 1))),         # gross margin improving
            gt(ratio(g(rev, 0), g(assets, 0)), ratio(g(rev, 1), g(assets, 1))), # asset turnover improving
        ])
        m["fscore"] = int(pts)

    # Altman Z components (market value of equity is added later, from today's market cap)
    ta = g(assets, 0)
    if ta and ta > 0:
        wc = (g(cur_a, 0) - g(cur_l, 0)) if g(cur_a, 0) is not None and g(cur_l, 0) is not None else None
        m["_z"] = {
            "wc": wc / ta if wc is not None else None,
            "re": (g(re, 0) or 0) / ta,
            "ebit": (g(ebit, 0) or 0) / ta,
            "sales": (g(rev, 0) or 0) / ta,
            "tl": g(liab, 0),
        }

    m["fy_end"] = inc.columns[0].date().isoformat() if inc is not None and len(inc.columns) else None
    return {k: (rnd(v) if isinstance(v, float) else v) for k, v in m.items()}


def fetch_statements(ticker: str) -> dict:
    import yfinance as yf

    t = yf.Ticker(ticker)
    inc = with_retries(lambda: t.income_stmt)
    bal = with_retries(lambda: t.balance_sheet)
    cf = with_retries(lambda: t.cashflow)
    return statement_metrics(inc, bal, cf)


# ---------------------------------------------------------------------------
# info (TTM snapshot) and prices
# ---------------------------------------------------------------------------

def fetch_info(ticker: str) -> dict:
    import yfinance as yf

    return with_retries(lambda: yf.Ticker(ticker).info) or {}


def info_fields(info: dict) -> dict:
    mcap = num(info.get("marketCap"))
    fcf = num(info.get("freeCashflow"))
    rev = num(info.get("totalRevenue"))
    de = num(info.get("debtToEquity"))
    pe_y, peg_y = num(info.get("trailingPE")), num(info.get("trailingPegRatio") or info.get("pegRatio"))
    debt, cash, ebitda = num(info.get("totalDebt")), num(info.get("totalCash")), num(info.get("ebitda"))
    ev = num(info.get("enterpriseValue"))
    price = num(info.get("currentPrice") or info.get("regularMarketPrice"))
    return {
        "name": info.get("shortName") or info.get("longName"),
        "sector": info.get("sector") or "Unknown",
        "industry": info.get("industry"),
        "price": price,
        "mcap": mcap,
        "fpe": num(info.get("forwardPE")),
        "ps": num(info.get("priceToSalesTrailing12Months")),
        "roe": num(info.get("returnOnEquity")),
        "gm": num(info.get("grossMargins")),
        "om": num(info.get("operatingMargins")),
        "fcfm": ratio(fcf, rev),
        "de": de / 100 if de is not None else None,  # Yahoo reports D/E in percent
        "nd_ebitda": ratio((debt or 0) - (cash or 0), ebitda) if ebitda and ebitda > 0 else None,
        "cur_ratio": num(info.get("currentRatio")),
        "rev_q": num(info.get("revenueGrowth")),
        "beta": num(info.get("beta")),
        "rec": num(info.get("recommendationMean")),
        "analysts": num(info.get("numberOfAnalystOpinions")),
        "insider": num(info.get("heldPercentInsiders")),
        "inst": num(info.get("heldPercentInstitutions")),
        "short": num(info.get("shortPercentOfFloat")),
        "divy": num(info.get("trailingAnnualDividendYield")),
        # base values so price-dependent ratios can be recomputed hourly
        "_b": {
            "eps": num(info.get("trailingEps")),
            "fcf": fcf,
            "rev": rev,
            "ebitda": ebitda,
            "ev_less_mcap": (ev - mcap) if ev is not None and mcap is not None else None,
            "peg_g": (pe_y / peg_y) if pe_y and peg_y and peg_y > 0 else None,  # implied growth % behind Yahoo's PEG
            "fpe_eps": (price / num(info.get("forwardPE"))) if price and num(info.get("forwardPE")) else None,
            "target": num(info.get("targetMeanPrice")),
            "mcap_px": price,
        },
    }


def price_metrics(close: pd.Series) -> dict:
    s = close.dropna()
    if len(s) < 30:
        return {}
    p = float(s.iloc[-1])

    def back(n):
        return float(s.iloc[-n - 1]) if len(s) > n else None

    yr = s.iloc[-253:]
    rets = np.log(yr / yr.shift(1)).dropna()
    peak = yr.cummax()
    weekly = s.iloc[-260:].resample("W-FRI").last().dropna().iloc[-53:]
    m = {
        "price": p,
        "chg1d": ratio(p, back(1)) - 1 if back(1) else None,
        "perf3m": ratio(p, back(63)) - 1 if back(63) else None,
        "perf6m": ratio(p, back(126)) - 1 if back(126) else None,
        "perf12m": ratio(p, back(252)) - 1 if back(252) else None,
        "mom": ratio(back(21), back(252)) - 1 if back(252) else None,
        "dma200": p / float(s.iloc[-200:].mean()) - 1 if len(s) >= 200 else None,
        "off_high": p / float(yr.max()) - 1,
        "vol": float(rets.std() * math.sqrt(252)) if len(rets) > 20 else None,
        "mdd": float((yr / peak - 1).min()),
        "_p": {"prev": back(1), "p63": back(63), "p126": back(126), "p252": back(252),
               "hi52": float(yr.max()), "ma200": float(s.iloc[-200:].mean()) if len(s) >= 200 else None},
        "_spark": [round(float(v), 2) for v in weekly],
        "_dates": [weekly.index[0].date().isoformat(), weekly.index[-1].date().isoformat()] if len(weekly) else None,
    }
    return m


def download_prices(tickers: list[str], period="14mo") -> pd.DataFrame:
    import yfinance as yf

    frames = []
    for i in range(0, len(tickers), 200):
        chunk = tickers[i:i + 200]
        df = with_retries(lambda: yf.download(chunk, period=period, interval="1d", auto_adjust=True,
                                              progress=False, threads=True, group_by="column"))
        if df is None or df.empty:
            continue
        close = df["Close"] if isinstance(df.columns, pd.MultiIndex) else df[["Close"]].rename(columns={"Close": chunk[0]})
        frames.append(close)
    return pd.concat(frames, axis=1) if frames else pd.DataFrame()


# ---------------------------------------------------------------------------
# derived ratios, scoring, screening
# ---------------------------------------------------------------------------

def derive(rec: dict) -> None:
    """Recompute everything that depends on the current price. Used by both full and hourly runs."""
    b, p = rec.get("_b", {}), rec.get("_p", {})
    price = num(rec.get("price"))
    if price is None:
        return
    if b.get("mcap_px") and rec.get("mcap"):
        rec["mcap"] = rec["mcap"] * price / b["mcap_px"]
        b["mcap_px"] = price
    mcap = rec.get("mcap")
    eps = b.get("eps")
    rec["pe"] = price / eps if eps and eps > 0 else None
    rec["peg"] = rec["pe"] / b["peg_g"] if rec["pe"] and b.get("peg_g") and b["peg_g"] > 0 else None
    if b.get("fpe_eps") and b["fpe_eps"] > 0:
        rec["fpe"] = price / b["fpe_eps"]
    rec["fcf_yield"] = ratio(b.get("fcf"), mcap)
    if b.get("rev") and mcap:
        rec["ps"] = mcap / b["rev"]
    if b.get("ebitda") and b["ebitda"] > 0 and mcap and b.get("ev_less_mcap") is not None:
        rec["ev_ebitda"] = (mcap + b["ev_less_mcap"]) / b["ebitda"]
    rec["upside"] = b["target"] / price - 1 if b.get("target") else None
    if p:
        for k, ref in (("chg1d", "prev"), ("perf3m", "p63"), ("perf6m", "p126"), ("perf12m", "p252")):
            if p.get(ref):
                rec[k] = price / p[ref] - 1
        if p.get("hi52"):
            p["hi52"] = max(p["hi52"], price)
            rec["off_high"] = price / p["hi52"] - 1
        if p.get("ma200"):
            rec["dma200"] = price / p["ma200"] - 1
    z = rec.get("_z")
    if z and z.get("tl") and z["tl"] > 0 and mcap and z.get("wc") is not None:
        rec["zscore"] = 1.2 * z["wc"] + 1.4 * z["re"] + 3.3 * z["ebit"] + 0.6 * mcap / z["tl"] + 1.0 * z["sales"]


def criteria_spec() -> list[dict]:
    return [dict(key=k, group=g, label=l, op=op, value=v, on=on, fmt=f, help=h)
            for k, g, l, op, v, on, f, h in CRITERIA]


def check(rec: dict, c: dict):
    """True / False / None (no data)."""
    if c["op"] == "pe_or_peg":
        pe, peg = rec.get("pe"), rec.get("peg")
        if pe is None and peg is None:
            return None
        return bool((pe is not None and 0 < pe <= c["value"][0]) or (peg is not None and 0 < peg <= c["value"][1]))
    v = rec.get(c["key"])
    if v is None:
        return None
    return v >= c["value"] if c["op"] == "min" else v <= c["value"]


def passes(rec: dict, crit: list[dict], excluded=EXCLUDE_SECTORS) -> bool:
    if rec.get("sector") in excluded:
        return False
    return all(check(rec, c) is True for c in crit if c["on"])


def score_all(stocks: list[dict]) -> None:
    """Composite 0-100: quality 35%, growth 25%, value 25%, momentum 15%, by percentile within eligible names."""
    elig = [s for s in stocks if s.get("sector") not in EXCLUDE_SECTORS and (s.get("mcap") or 0) >= 1e9]
    if not elig:
        return

    def pct(key, fn=lambda v: v):
        vals = sorted(fn(s[key]) for s in elig if s.get(key) is not None)
        out = {}
        for s in elig:
            v = s.get(key)
            out[s["t"]] = 0.25 if v is None or not vals else float(np.searchsorted(vals, fn(v), side="right")) / len(vals)
        return out

    ey = {s["t"]: (1 / s["pe"] if s.get("pe") else None) for s in elig}
    for s in elig:
        s["_ey"] = ey[s["t"]]
    P = {k: pct(k) for k in ("roic_avg", "roic", "gm", "fcfm", "fscore", "rev3y", "eps3y", "_ey", "fcf_yield", "mom")}
    for s in elig:
        t = s["t"]
        roic_p = P["roic_avg"][t] if s.get("roic_avg") is not None else P["roic"][t]
        q = np.mean([roic_p, P["gm"][t], P["fcfm"][t], P["fscore"][t]])
        g = np.mean([P["rev3y"][t], P["eps3y"][t]])
        v = np.mean([P["_ey"][t], P["fcf_yield"][t]])
        m = P["mom"][t]
        s["sub"] = {"quality": round(q * 100), "growth": round(g * 100), "value": round(v * 100), "momentum": round(m * 100)}
        s["score"] = round((0.35 * q + 0.25 * g + 0.25 * v + 0.15 * m) * 100)
        s.pop("_ey", None)
    for s in stocks:
        s.pop("_ey", None)


COVERAGE_KEYS = [c[0] for c in CRITERIA if c[5]]


def finalize(stocks: list[dict]) -> None:
    for s in stocks:
        derive(s)
        have = sum((s.get("pe") is not None or s.get("peg") is not None) if k == "pe_or_peg" else s.get(k) is not None
                   for k in COVERAGE_KEYS)
        s["coverage"] = round(have / len(COVERAGE_KEYS), 2)
    score_all(stocks)
    for s in stocks:
        for k, v in list(s.items()):
            if isinstance(v, float):
                s[k] = rnd(v)
            elif isinstance(v, dict):
                s[k] = {kk: sig(vv) for kk, vv in v.items()}


def update_history(stocks: list[dict], crit: list[dict], when: str) -> dict:
    hist = load_json(DATA / "history.json", {"days": []})
    if load_json(DATA / "screen.json", {}).get("demo"):
        hist = {"days": []}  # don't carry sample-data history into the real record
    day = when[:10]
    tickers = sorted(s["t"] for s in stocks if passes(s, crit))
    days = [d for d in hist["days"] if d["date"] != day] + [{"date": day, "n": len(tickers), "passes": tickers}]
    hist["days"] = sorted(days, key=lambda d: d["date"])[-HISTORY_DAYS:]
    return hist


def public_record(s: dict) -> dict:
    """What the dashboard gets: metrics plus the base values the hourly job needs."""
    return {k: v for k, v in s.items() if k not in ("_spark", "_dates", "revh")}


# ---------------------------------------------------------------------------
# runs
# ---------------------------------------------------------------------------

def run_full(limit: int | None = None) -> None:
    t0 = time.time()
    print("1/5 universe")
    uni = get_universe()
    tickers = sorted(uni)[:limit] if limit else sorted(uni)
    print(f"    {len(tickers)} tickers")

    print("2/5 prices (batched)")
    closes = download_prices(tickers)
    print(f"    {closes.shape[1]} price series")

    print("3/5 TTM snapshot (Yahoo info)")
    recs: dict[str, dict] = {}
    failed = []
    with ThreadPoolExecutor(WORKERS) as ex:
        futs = {ex.submit(fetch_info, t): t for t in tickers}
        for i, f in enumerate(as_completed(futs), 1):
            t = futs[f]
            try:
                info = f.result()
                if info.get("quoteType") not in (None, "EQUITY"):
                    continue
                recs[t] = {"t": t, "idx": uni[t], **info_fields(info)}
            except Exception as e:
                failed.append(t)
                print(f"    ! {t}: {type(e).__name__}", file=sys.stderr)
            if i % 250 == 0:
                print(f"    {i}/{len(tickers)}")

    for t, r in recs.items():
        if t in closes.columns:
            r.update(price_metrics(closes[t]))
        r["_b"]["mcap_px"] = r["_b"]["mcap_px"] or r.get("price")

    print("4/5 annual statements (cached weekly)")
    cache = load_json(CACHE / "fundamentals.json", {})
    today = dt.date.today()
    need = [t for t, r in recs.items()
            if r.get("sector") not in EXCLUDE_SECTORS and (r.get("mcap") or 0) >= STATEMENT_MIN_MCAP
            and (t not in cache or (today - dt.date.fromisoformat(cache[t]["_at"])).days >= STATEMENT_MAX_AGE_DAYS)]
    print(f"    {len(need)} to refresh, {len(cache)} cached")
    with ThreadPoolExecutor(WORKERS) as ex:
        futs = {ex.submit(fetch_statements, t): t for t in need}
        for i, f in enumerate(as_completed(futs), 1):
            t = futs[f]
            try:
                cache[t] = {**f.result(), "_at": today.isoformat()}
            except Exception as e:
                print(f"    ! statements {t}: {type(e).__name__}", file=sys.stderr)
            if i % 250 == 0:
                print(f"    {i}/{len(need)}")
                save_json(CACHE / "fundamentals.json", cache)
    save_json(CACHE / "fundamentals.json", cache)
    for t, r in recs.items():
        if t in cache:
            r.update({k: v for k, v in cache[t].items() if k != "_at"})

    print("5/5 scoring and writing")
    stocks = sorted(recs.values(), key=lambda r: r["t"])
    finalize(stocks)
    crit = criteria_spec()
    when = now_utc()
    detail = {s["t"]: {"spark": s.get("_spark"), "dates": s.get("_dates"), "revh": s.get("revh")} for s in stocks}
    history = update_history(stocks, crit, when)
    save_json(DATA / "screen.json", {
        "generated": when, "prices_updated": when, "demo": False,
        "universe": "S&P 500 + 400 + 600", "excluded_sectors": EXCLUDE_SECTORS,
        "criteria": criteria_spec(), "display": DISPLAY,
        "stats": {"tickers": len(tickers), "loaded": len(stocks), "failed": len(failed)},
        "stocks": [public_record(s) for s in stocks],
    }, records=True)
    save_json(DATA / "detail.json", detail)
    save_json(DATA / "history.json", history)
    n = sum(passes(s, crit) for s in stocks)
    print(f"done in {time.time() - t0:.0f}s: {len(stocks)} stocks, {n} pass the default screen, {len(failed)} failed")
    if len(stocks) < 0.5 * len(tickers):
        raise SystemExit("Fewer than half the tickers loaded; Yahoo may be rate limiting. Data was still written.")


def run_prices() -> None:
    screen = load_json(DATA / "screen.json", None)
    if not screen or screen.get("demo"):
        raise SystemExit("No real screen.json yet. Run `python screener.py full` first.")
    stocks = screen["stocks"]
    closes = download_prices([s["t"] for s in stocks], period="5d")
    updated = 0
    for s in stocks:
        if s["t"] not in closes.columns:
            continue
        col = closes[s["t"]].dropna()
        if col.empty:
            continue
        last_day = col.index[-1].date()
        s["price"] = float(col.iloc[-1])
        # once a new session starts, the previous close moves forward
        if len(col) >= 2 and s.get("_p") and str(last_day) > (screen.get("generated") or "")[:10]:
            s["_p"]["prev"] = float(col.iloc[-2])
        updated += 1
    finalize(stocks)
    screen["prices_updated"] = now_utc()
    save_json(DATA / "screen.json", screen, records=True)
    print(f"refreshed {updated}/{len(stocks)} prices")


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("mode", choices=["full", "prices"], nargs="?", default="full")
    ap.add_argument("--limit", type=int, help="only the first N tickers (for a quick test)")
    a = ap.parse_args(argv)
    run_full(a.limit) if a.mode == "full" else run_prices()


if __name__ == "__main__":
    main()
