#!/usr/bin/env python3
"""Long-hold stock screener: quality at a reasonable price, global universe.

Two modes:
  python screener.py full     # fundamentals + prices for the whole universe (daily, after the US close)
  python screener.py prices   # refresh prices and price-based ratios only (hourly on weekdays)

Universe: S&P 500 + 400 + 600 in the US, plus every stock worth INTL_MIN_MCAP_USD or more on the main
exchange of ~50 other countries. All money figures are converted to US dollars.

Writes the dashboard's data files to docs/data/ (or $LS_DATA_DIR). Data comes from Yahoo Finance via
yfinance (free). Edit CRITERIA below to change the default screen; the dashboard reads its defaults from here.
"""
from __future__ import annotations

import argparse
import datetime as dt
import io
import json
import math
import os
import re
import sys
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

import numpy as np
import pandas as pd
import requests

ROOT = Path(__file__).resolve().parent
DATA = Path(os.environ.get("LS_DATA_DIR") or ROOT / "docs" / "data")
CACHE = Path(os.environ.get("LS_CACHE_DIR") or ROOT / "cache")

# ---------------------------------------------------------------------------
# CONFIG
# ---------------------------------------------------------------------------
EXCLUDE_SECTORS = ["Financial Services", "Real Estate"]  # D/E, ROE and FCF mean different things there
EXTRA_TICKERS: list[str] = []          # anything else, using Yahoo symbols, e.g. ["ASML", "7203.T", "MC.PA"]
INTERNATIONAL = True                   # False = US only (S&P 1500)
INTL_MIN_MCAP_USD = 2e9                # international stocks below this aren't loaded at all
STATEMENT_MAX_AGE_DAYS = 7             # annual statements are re-pulled at most weekly
STATEMENT_MIN_MCAP = 1e9               # skip statement pulls for tiny names (saves requests)
STATEMENT_BUDGET = 2500                # max companies whose statements are pulled per run (biggest first)
TIME_BUDGET_MIN = 270                  # stop starting new statement pulls after this, so the run always finishes
WORKERS = 6                            # parallel Yahoo requests; lower it if you get rate limited
HISTORY_DAYS = 400

# Main exchange(s) per country, as Yahoo names them. Delete a line to drop that country.
INTL_EXCHANGES = {
    "ca": ["TOR"], "gb": ["LSE"], "ie": ["ISE"],
    "de": ["GER"], "fr": ["PAR"], "nl": ["AMS"], "be": ["BRU"], "ch": ["EBS"], "at": ["VIE"], "it": ["MIL"],
    "es": ["MCE"], "pt": ["LIS"], "se": ["STO"], "dk": ["CPH"], "no": ["OSL"], "fi": ["HEL"], "is": ["ICE"],
    "pl": ["WSE"], "cz": ["PRA"], "hu": ["BUD"], "gr": ["ATH"], "ro": ["BVB"],
    "jp": ["JPX"], "cn": ["SHH", "SHZ"], "hk": ["HKG"], "in": ["NSI"], "kr": ["KSC", "KOE"], "tw": ["TAI", "TWO"],
    "au": ["ASX"], "nz": ["NZE"], "sg": ["SES"], "my": ["KLS"], "th": ["SET"], "id": ["JKT"], "ph": ["PHS"],
    "vn": ["VSE"],
    "br": ["SAO"], "mx": ["MEX"], "cl": ["SGO"], "co": ["BVC"], "ar": ["BUE"],
    "il": ["TLV"], "tr": ["IST"], "za": ["JNB"], "sa": ["SAU"], "ae": ["DFM"], "qa": ["DOH"], "kw": ["KUW"],
    "eg": ["CAI"],
}

# Dashboard market groups
_EUROPE = ["de", "fr", "nl", "be", "ch", "at", "it", "es", "pt", "se", "dk", "no", "fi", "is", "pl", "cz", "hu", "gr", "ro"]
_APAC = ["kr", "tw", "au", "nz", "sg", "my", "th", "id", "ph", "vn"]
_LATAM = ["br", "mx", "cl", "co", "ar"]
_MEA = ["il", "tr", "za", "sa", "ae", "qa", "kw", "eg"]
MARKET_OF_REGION = {"us": "US", "ca": "Canada", "gb": "UK & Ireland", "ie": "UK & Ireland", "jp": "Japan",
                    "cn": "China & HK", "hk": "China & HK", "in": "India",
                    **{r: "Europe" for r in _EUROPE}, **{r: "Asia-Pacific" for r in _APAC},
                    **{r: "Latin America" for r in _LATAM}, **{r: "Middle East & Africa" for r in _MEA}}
COUNTRY_MARKET = {
    "United States": "US", "Canada": "Canada", "United Kingdom": "UK & Ireland", "Ireland": "UK & Ireland",
    "Japan": "Japan", "China": "China & HK", "Hong Kong": "China & HK", "India": "India",
    **{c: "Europe" for c in ["Germany", "France", "Netherlands", "Belgium", "Switzerland", "Austria", "Italy", "Spain",
                             "Portugal", "Sweden", "Denmark", "Norway", "Finland", "Iceland", "Poland",
                             "Czech Republic", "Hungary", "Greece", "Romania"]},
    **{c: "Asia-Pacific" for c in ["South Korea", "Taiwan", "Australia", "New Zealand", "Singapore", "Malaysia",
                                   "Thailand", "Indonesia", "Philippines", "Vietnam"]},
    **{c: "Latin America" for c in ["Brazil", "Mexico", "Chile", "Colombia", "Argentina", "Peru"]},
    **{c: "Middle East & Africa" for c in ["Israel", "Turkey", "South Africa", "Saudi Arabia", "United Arab Emirates",
                                           "Qatar", "Kuwait", "Egypt"]},
}
# Currencies needed up front (to size up international listings); others are fetched as they appear
BASE_CCYS = ["CAD", "GBP", "EUR", "CHF", "SEK", "NOK", "DKK", "ISK", "PLN", "CZK", "HUF", "RON", "JPY", "CNY",
             "HKD", "INR", "KRW", "TWD", "AUD", "NZD", "SGD", "MYR", "THB", "IDR", "PHP", "VND", "BRL", "MXN",
             "CLP", "COP", "ARS", "ILS", "TRY", "ZAR", "SAR", "AED", "QAR", "KWD", "EGP"]
MINOR_CCY = {"GBp": ("GBP", 0.01), "GBX": ("GBP", 0.01), "ZAc": ("ZAR", 0.01), "ILA": ("ILS", 0.01)}

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


def us_universe() -> dict[str, dict]:
    """{ticker: {"region": "us", "idx": index}}. Falls back to the last good copy if Wikipedia is unreachable."""
    cache_file = CACHE / "universe_us.json"
    out: dict[str, dict] = {}
    headers = {"User-Agent": "Mozilla/5.0 (long-screener; personal use)"}
    for idx, url in WIKI.items():
        try:
            html = requests.get(url, headers=headers, timeout=30).text
            for table in pd.read_html(io.StringIO(html)):
                col = next((c for c in table.columns if str(c).lower() in ("symbol", "ticker", "ticker symbol")), None)
                if col is not None:
                    for t in table[col].dropna().astype(str):
                        out.setdefault(t.strip().replace(".", "-"), {"region": "us", "idx": idx})
                    break
        except Exception as e:
            print(f"  ! could not load {idx} list: {e}", file=sys.stderr)
    if len(out) > 1000:
        save_json(cache_file, out)
        return out
    cached = load_json(cache_file, {}) or {t: {"region": "us", "idx": i} for t, i in load_json(CACHE / "universe.json", {}).items()}
    if cached:
        print(f"  using cached US universe ({len(cached)} tickers)")
        return cached
    if out:
        return out
    raise SystemExit("No US universe available: Wikipedia unreachable and no cached copy yet.")


def base_ccy(cur: str | None) -> tuple[str, float]:
    cur = cur or "USD"
    return MINOR_CCY.get(cur, (cur, 1.0))


def fx_rates(currencies, known: dict | None = None) -> dict[str, float]:
    """US dollars per unit of each currency (minor units like GBp handled). Cached for when Yahoo hiccups."""
    rates = dict(known or {})
    rates["USD"] = 1.0
    need = sorted({base_ccy(c)[0] for c in currencies if c} - set(rates))
    if need:
        try:
            closes = download_prices([f"{c}USD=X" for c in need], period="5d")
            for c in need:
                col = f"{c}USD=X"
                if col in closes.columns and closes[col].dropna().size:
                    rates[c] = float(closes[col].dropna().iloc[-1])
        except Exception as e:
            print(f"  ! FX download failed: {e}", file=sys.stderr)
        cached = load_json(CACHE / "fx.json", {})
        for c in need:
            if c not in rates and c in cached:
                rates[c] = cached[c]
        save_json(CACHE / "fx.json", {**cached, **{k: v for k, v in rates.items() if k not in MINOR_CCY}})
    for c in currencies:
        if c in MINOR_CCY and MINOR_CCY[c][0] in rates:
            rates[c] = rates[MINOR_CCY[c][0]] * MINOR_CCY[c][1]
    return rates


def intl_universe(fx: dict) -> dict[str, dict]:
    """Every listing on each country's main exchange worth roughly INTL_MIN_MCAP_USD or more.

    Uses Yahoo's screener sorted by market cap, and stops paging once listings fall well below the floor.
    The floor is applied loosely here (half of it, major-currency units) and exactly once per-stock data is in.
    """
    import yfinance as yf

    EQ = yf.EquityQuery
    out: dict[str, dict] = {}
    floor = INTL_MIN_MCAP_USD * 0.5
    for region, exchanges in INTL_EXCHANGES.items():
        q = EQ("and", [EQ("eq", ["region", region]), EQ("is-in", ["exchange", *exchanges])])
        offset, n = 0, 0
        while True:
            try:
                res = with_retries(lambda: yf.screen(q, offset=offset, size=250, sortField="intradaymarketcap", sortAsc=False))
            except Exception as e:
                print(f"  ! {region}: screener failed at offset {offset}: {type(e).__name__}", file=sys.stderr)
                break
            quotes = res.get("quotes") or []
            last_usd = None
            for qt in quotes:
                cur, mult = base_ccy(qt.get("currency"))
                rate = fx.get(cur)
                mc = num(qt.get("marketCap"))
                if rate is None or mc is None:
                    continue
                last_usd = mc * rate  # loose: treats minor units (pence) as major, so nothing is cut wrongly
                if last_usd >= floor and qt.get("quoteType", "EQUITY") == "EQUITY" and qt.get("symbol"):
                    out[qt["symbol"]] = {"region": region}
                    n += 1
            offset += len(quotes)
            if not quotes or offset >= (res.get("total") or 0) or (last_usd is not None and last_usd < floor):
                break
            time.sleep(0.3)
        print(f"    {region}: {n}")
    return out


_SUFFIXES = {"inc", "incorporated", "corp", "corporation", "co", "company", "ltd", "limited", "plc", "ag", "sa", "se",
             "nv", "bv", "ab", "asa", "as", "oyj", "spa", "sab", "de", "cv", "kk", "tbk", "bhd", "berhad", "pcl",
             "holdings", "holding", "group", "the", "adr", "class", "a", "b", "ord", "shs", "reg", "registered"}


def norm_name(name: str | None) -> str:
    words = re.sub(r"[^a-z0-9 ]+", " ", (name or "").lower()).split()
    while words and words[-1] in _SUFFIXES:
        words.pop()
    return " ".join(w for w in words if w != "the")


def _family(market: str | None) -> str | None:
    return "Europe" if market in ("Europe", "UK & Ireland") else market


def dedupe(recs: dict[str, dict]) -> dict[str, dict]:
    """One listing per company. Drops foreign listings of companies whose home market is covered elsewhere
    (e.g. US stocks on Mexico's exchange), then keeps the best listing among same-name duplicates."""
    keep = {}
    for t, r in recs.items():
        home = COUNTRY_MARKET.get(r.get("country"))
        if r.get("region") not in ("us", "extra") and home and _family(home) != _family(r.get("mkt")):
            continue
        keep[t] = r
    best: dict[str, str] = {}
    for t, r in keep.items():
        key = norm_name(r.get("name")) or t
        if key not in best:
            best[key] = t
            continue
        o = keep[best[key]]

        def rank(x):
            return (x.get("region") in ("us", "extra"), COUNTRY_MARKET.get(x.get("country")) == x.get("mkt"), x.get("_adv") or 0)

        if rank(r) > rank(o):
            best[key] = t
    winners = set(best.values())
    return {t: r for t, r in keep.items() if t in winners}


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
# info (TTM snapshot), currency handling and prices
# ---------------------------------------------------------------------------

def fetch_info(ticker: str) -> dict:
    import yfinance as yf

    return with_retries(lambda: yf.Ticker(ticker).info) or {}


def _closest(cands: list, target):
    """Pick the candidate nearest the target on a log scale (used to detect which currency Yahoo used)."""
    cands = [c for c in cands if c is not None and c > 0]
    if not cands:
        return None
    if target is None or target <= 0:
        return cands[0]
    return min(cands, key=lambda c: abs(math.log(c / target)))


def info_fields(info: dict, fx: dict) -> dict:
    """Normalise one Yahoo info dict. Prices stay in the listing currency; money totals become US dollars.

    Yahoo is not consistent about currencies (pence vs pounds, ADRs priced in USD but reporting in TWD...),
    so where it gives us a cross-check (its own P/E and P/S) we use it to pick the right conversion.
    """
    cur = info.get("currency") or "USD"
    fin = info.get("financialCurrency") or base_ccy(cur)[0]
    r_cur, r_fin = fx.get(cur), fx.get(fin)
    price = num(info.get("currentPrice") or info.get("regularMarketPrice"))
    out = {
        "name": info.get("shortName") or info.get("longName"),
        "sector": info.get("sector") or "Unknown",
        "industry": info.get("industry"),
        "country": info.get("country"),
        "cur": cur,
        "price": price,
        "fpe": num(info.get("forwardPE")),
        "roe": num(info.get("returnOnEquity")),
        "gm": num(info.get("grossMargins")),
        "om": num(info.get("operatingMargins")),
        "cur_ratio": num(info.get("currentRatio")),
        "rev_q": num(info.get("revenueGrowth")),
        "beta": num(info.get("beta")),
        "rec": num(info.get("recommendationMean")),
        "analysts": num(info.get("numberOfAnalystOpinions")),
        "insider": num(info.get("heldPercentInsiders")),
        "inst": num(info.get("heldPercentInstitutions")),
        "short": num(info.get("shortPercentOfFloat")),
        "divy": num(info.get("trailingAnnualDividendYield")),
    }
    de = num(info.get("debtToEquity"))
    out["de"] = de / 100 if de is not None else None  # Yahoo reports D/E in percent
    fcf, rev = num(info.get("freeCashflow")), num(info.get("totalRevenue"))
    debt, cash, ebitda = num(info.get("totalDebt")), num(info.get("totalCash")), num(info.get("ebitda"))
    out["fcfm"] = ratio(fcf, rev)
    out["nd_ebitda"] = ratio((debt or 0) - (cash or 0), ebitda) if ebitda and ebitda > 0 else None
    if r_cur is None or r_fin is None:
        out["_b"] = {}
        out["mcap"] = None  # can't convert: the market-cap test will report "no data"
        return out

    # market cap in USD: Yahoo's figure, unless shares x price disagrees wildly (pence/pounds mix-ups)
    mc = num(info.get("marketCap"))
    shares = num(info.get("sharesOutstanding") or info.get("impliedSharesOutstanding"))
    from_shares = shares * price * r_cur if shares and price else None
    cands = [mc * r_cur if mc else None, mc * fx.get(base_ccy(cur)[0], r_cur) if mc else None]
    mcap = _closest(cands, from_shares)
    if from_shares and (mcap is None or abs(math.log(mcap / from_shares)) > math.log(3)):
        mcap = from_shares
    out["mcap"] = mcap

    # statement-currency totals -> USD. P/S cross-check tells us if Yahoo already used the listing currency
    ps_y = num(info.get("priceToSalesTrailing12Months"))
    r_money = r_fin
    if rev and mcap and ps_y and ps_y > 0:
        r_money = _closest([r_fin, r_cur], mcap / ps_y / rev) or r_fin
    usd = (lambda v: v * r_money if v is not None else None)
    # EPS -> listing currency. P/E cross-check catches ADRs where Yahoo quotes per-ADR EPS in dollars
    eps, pe_y = num(info.get("trailingEps")), num(info.get("trailingPE"))
    if eps is not None:
        eps_conv = eps * r_fin / r_cur
        if pe_y and price and eps > 0:
            eps = _closest([eps, eps_conv], price / pe_y)
        else:
            eps = eps_conv
    fpe = num(info.get("forwardPE"))
    peg_y = num(info.get("trailingPegRatio") or info.get("pegRatio"))
    out["_adv"] = (num(info.get("averageVolume")) or 0) * (price or 0) * r_cur
    out["_b"] = {
        "eps": eps,                                                         # listing currency
        "fcf": usd(fcf), "rev": usd(rev), "ebitda": usd(ebitda),            # USD
        "net_debt": usd((debt or 0) - (cash or 0)) if debt is not None or cash is not None else None,
        "peg_g": (pe_y / peg_y) if pe_y and peg_y and peg_y > 0 else None,  # implied growth % behind Yahoo's PEG
        "fpe_eps": (price / fpe) if price and fpe else None,               # listing currency
        "target": num(info.get("targetMeanPrice")),                         # listing currency
        "mcap_px": price,
        "r_fin": r_fin,
    }
    return out


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
    b, p = rec.get("_b") or {}, rec.get("_p") or {}
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
    rec["ps"] = ratio(mcap, b.get("rev")) if b.get("rev") and b["rev"] > 0 else None
    if b.get("ebitda") and b["ebitda"] > 0 and mcap and b.get("net_debt") is not None:
        rec["ev_ebitda"] = (mcap + b["net_debt"]) / b["ebitda"]
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
    if z and z.get("tl_usd") and z["tl_usd"] > 0 and mcap and z.get("wc") is not None:
        rec["zscore"] = 1.2 * z["wc"] + 1.4 * z["re"] + 3.3 * z["ebit"] + 0.6 * mcap / z["tl_usd"] + 1.0 * z["sales"]


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
PRIVATE = ("_b", "_p", "_z", "_adv")          # kept in state.json for the hourly job, not sent to the browser
DETAIL = ("_spark", "_dates", "revh")         # detail.json, loaded by the dashboard on demand


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
                s[k] = sig(v) if abs(v) >= 1000 else rnd(v)
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


def write_outputs(head: dict, stocks: list[dict], detail: dict | None = None) -> None:
    public = [{k: v for k, v in s.items() if k not in PRIVATE and k not in DETAIL} for s in stocks]
    state = {s["t"]: {k: s[k] for k in PRIVATE if k in s} for s in stocks}
    markets = sorted({s.get("mkt") for s in stocks if s.get("mkt")})
    save_json(DATA / "screen.json", {**head, "criteria": criteria_spec(), "display": DISPLAY, "markets": markets,
                                     "excluded_sectors": EXCLUDE_SECTORS, "stocks": public}, records=True)
    save_json(DATA / "state.json", state)
    if detail is not None:
        save_json(DATA / "detail.json", detail)


# ---------------------------------------------------------------------------
# runs
# ---------------------------------------------------------------------------

def run_full(limit: int | None = None) -> None:
    t0 = time.time()
    print("1/6 currencies and universe")
    fx = fx_rates(BASE_CCYS if INTERNATIONAL else [])
    uni = us_universe()
    for t in EXTRA_TICKERS:
        uni.setdefault(t, {"region": "extra", "idx": "Extra"})
    if INTERNATIONAL:
        for t, v in intl_universe(fx).items():
            uni.setdefault(t, v)
    tickers = sorted(uni)[:limit] if limit else sorted(uni)
    print(f"    {len(tickers)} listings")

    print("2/6 prices (batched)")
    closes = download_prices(tickers)
    print(f"    {closes.shape[1]} price series")

    print("3/6 TTM snapshot (Yahoo info)")
    infos: dict[str, dict] = {}
    failed = []
    with ThreadPoolExecutor(WORKERS) as ex:
        futs = {ex.submit(fetch_info, t): t for t in tickers}
        for i, f in enumerate(as_completed(futs), 1):
            t = futs[f]
            try:
                info = f.result()
                if info.get("quoteType") in (None, "EQUITY"):
                    infos[t] = info
            except Exception as e:
                failed.append(t)
                print(f"    ! {t}: {type(e).__name__}", file=sys.stderr)
            if i % 500 == 0:
                print(f"    {i}/{len(tickers)}  ({time.time() - t0:.0f}s)")
    fx = fx_rates({c for i in infos.values() for c in (i.get("currency"), i.get("financialCurrency"))} - {None}, fx)

    recs: dict[str, dict] = {}
    for t, info in infos.items():
        u = uni[t]
        r = {"t": t, "idx": u.get("idx") or (info.get("fullExchangeName") or info.get("exchange")),
             "region": u["region"], **info_fields(info, fx)}
        r["mkt"] = MARKET_OF_REGION.get(u["region"]) or COUNTRY_MARKET.get(r.get("country")) or "Other"
        if u["region"] not in ("us", "extra") and (r.get("mcap") or 0) < INTL_MIN_MCAP_USD:
            continue
        recs[t] = r
    before = len(recs)
    recs = dedupe(recs)
    print(f"    {len(recs)} stocks after removing {before - len(recs)} duplicate/foreign listings")

    for t, r in recs.items():
        if t in closes.columns:
            r.update(price_metrics(closes[t]))
        if r.get("_b") is not None:
            r["_b"]["mcap_px"] = r["_b"].get("mcap_px") or r.get("price")

    print("4/6 annual statements (cached weekly, biggest companies first)")
    cache = load_json(CACHE / "fundamentals.json", {})
    today = dt.date.today()

    def age(t):
        return (today - dt.date.fromisoformat(cache[t]["_at"])).days if t in cache else 10 ** 6

    eligible = [t for t, r in recs.items()
                if r.get("sector") not in EXCLUDE_SECTORS and (r.get("mcap") or 0) >= STATEMENT_MIN_MCAP
                and age(t) >= STATEMENT_MAX_AGE_DAYS]
    eligible.sort(key=lambda t: (t in cache, -age(t), -(recs[t].get("mcap") or 0)))  # missing first, then stalest
    need = eligible[:STATEMENT_BUDGET]
    print(f"    {len(eligible)} due, pulling {len(need)} this run, {len(cache)} cached")
    deadline = t0 + TIME_BUDGET_MIN * 60
    done = 0
    with ThreadPoolExecutor(WORKERS) as ex:
        for start in range(0, len(need), 100):
            if time.time() > deadline:
                print(f"    time budget reached; the rest will be pulled on the next run")
                break
            batch = need[start:start + 100]
            futs = {ex.submit(fetch_statements, t): t for t in batch}
            for f in as_completed(futs):
                t = futs[f]
                try:
                    cache[t] = {**f.result(), "_at": today.isoformat()}
                    done += 1
                except Exception as e:
                    print(f"    ! statements {t}: {type(e).__name__}", file=sys.stderr)
            if start % 500 == 0:
                print(f"    {start + len(batch)}/{len(need)}  ({time.time() - t0:.0f}s)")
                save_json(CACHE / "fundamentals.json", cache)
    save_json(CACHE / "fundamentals.json", cache)
    for t, r in recs.items():
        if t in cache:
            st = {k: v for k, v in cache[t].items() if k != "_at"}
            r_fin = (r.get("_b") or {}).get("r_fin")
            if r_fin:  # statements are in the reporting currency
                if st.get("revh"):
                    st["revh"] = [[y, round(v * r_fin)] for y, v in st["revh"]]
                if st.get("_z"):
                    st["_z"] = {**st["_z"], "tl_usd": (st["_z"].get("tl") or 0) * r_fin}
            r.update(st)

    print("5/6 scoring")
    stocks = sorted(recs.values(), key=lambda r: r["t"])
    finalize(stocks)
    crit = criteria_spec()
    when = now_utc()

    print("6/6 writing")
    detail = {s["t"]: {"spark": s.get("_spark"), "dates": s.get("_dates"), "revh": s.get("revh")} for s in stocks}
    history = update_history(stocks, crit, when)
    n_intl = sum(s["region"] != "us" for s in stocks)
    write_outputs({
        "generated": when, "prices_updated": when, "demo": False,
        "universe": "S&P 1500" + (f" + {n_intl:,} international" if n_intl else ""),
        "stats": {"listings": len(tickers), "loaded": len(stocks), "failed": len(failed),
                  "statements_cached": len(cache), "statements_pulled": done},
    }, stocks, detail)
    save_json(DATA / "history.json", history)
    n = sum(passes(s, crit) for s in stocks)
    print(f"done in {time.time() - t0:.0f}s: {len(stocks)} stocks, {n} pass the default screen, {len(failed)} failed")
    if len(infos) < 0.5 * len(tickers):
        raise SystemExit("Fewer than half the tickers loaded; Yahoo may be rate limiting. Data was still written.")


def run_prices() -> None:
    screen = load_json(DATA / "screen.json", None)
    if not screen or screen.get("demo"):
        raise SystemExit("No real screen.json yet. Run `python screener.py full` first.")
    state = load_json(DATA / "state.json", {})
    stocks = screen["stocks"]
    for s in stocks:
        s.update(state.get(s["t"], {}))
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
    head = {k: v for k, v in screen.items() if k not in ("stocks", "criteria", "display", "markets", "excluded_sectors")}
    head["prices_updated"] = now_utc()
    write_outputs(head, stocks)
    print(f"refreshed {updated}/{len(stocks)} prices")


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("mode", choices=["full", "prices"], nargs="?", default="full")
    ap.add_argument("--limit", type=int, help="only the first N tickers (for a quick test)")
    a = ap.parse_args(argv)
    run_full(a.limit) if a.mode == "full" else run_prices()


if __name__ == "__main__":
    main()
