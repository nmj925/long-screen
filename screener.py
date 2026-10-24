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
import gzip
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
STATEMENT_BUDGET = 1500                # max companies whose statements are pulled per run (missing first, then stalest)
INFO_MAX_AGE_DAYS = 3                  # TTM snapshot re-pulled every 3 days (prices and price ratios update every run)
INFO_BUDGET = 3500                     # max TTM snapshots pulled per run
TIME_BUDGET_MIN = 270                  # stop starting new pulls after this, so the run always finishes
WORKERS = 3                            # parallel Yahoo requests; lower it if you get rate limited
REQUEST_PAUSE = 0.3                    # seconds each worker waits between requests (keeps Yahoo from blocking us)
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

# Home country of each listing region (used to prefer a company's home listing over cross-listings)
REGION_COUNTRY = {
    "us": "United States", "ca": "Canada", "gb": "United Kingdom", "ie": "Ireland", "de": "Germany", "fr": "France",
    "nl": "Netherlands", "be": "Belgium", "ch": "Switzerland", "at": "Austria", "it": "Italy", "es": "Spain",
    "pt": "Portugal", "se": "Sweden", "dk": "Denmark", "no": "Norway", "fi": "Finland", "is": "Iceland", "pl": "Poland",
    "cz": "Czech Republic", "hu": "Hungary", "gr": "Greece", "ro": "Romania", "jp": "Japan", "cn": "China",
    "hk": "Hong Kong", "in": "India", "kr": "South Korea", "tw": "Taiwan", "au": "Australia", "nz": "New Zealand",
    "sg": "Singapore", "my": "Malaysia", "th": "Thailand", "id": "Indonesia", "ph": "Philippines", "vn": "Vietnam",
    "br": "Brazil", "mx": "Mexico", "cl": "Chile", "co": "Colombia", "ar": "Argentina", "il": "Israel",
    "tr": "Turkey", "za": "South Africa", "sa": "Saudi Arabia", "ae": "United Arab Emirates", "qa": "Qatar",
    "kw": "Kuwait", "eg": "Egypt",
}
# Venues that also list thousands of foreign companies. There, a listing whose accounts are in another
# currency is a cross-listing (the home listing is loaded from its own exchange), so it is skipped up front.
CROSS_LISTING_VENUES = {"de": "EUR", "at": "EUR", "mx": "MXN"}
MAX_SCREENER_PAGES = 80                # safety cap per country (80 x 250 listings)

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
        if path.suffix == ".gz":
            return json.loads(gzip.decompress(path.read_bytes()))
        return json.loads(path.read_text())
    except (OSError, ValueError, EOFError):
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
    tmp = path.with_name(path.name + ".tmp")
    if path.suffix == ".gz":
        tmp.write_bytes(gzip.compress(text.encode(), compresslevel=6))
    else:
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


class YahooRateLimitOrEmpty(Exception):
    """Yahoo answered with nothing useful. When it is rate limiting, it often does this instead of erroring."""


def fetch_many(fn, tickers: list[str], label: str, deadline: float, budget: int | None = None):
    """Fetch in small paced batches. If most of a batch fails, Yahoo is blocking us: back off and retry
    that batch, and give up for this run after repeated blocks (cached data is used for the rest).
    Stragglers that failed inside otherwise-good batches get one more try at the end."""
    results: dict[str, object] = {}
    todo = list(tickers[:budget] if budget else tickers)
    t0 = time.time()
    aborted = False

    def call(t):
        out = fn(t)
        time.sleep(REQUEST_PAUSE)
        return out

    def run(items: list[str], ex) -> list[str]:
        nonlocal aborted
        failed: list[str] = []
        strikes, i = 0, 0
        while i < len(items):
            if time.time() > deadline:
                print(f"    {label}: time budget reached at {i}/{len(items)}; the rest come from cache / next run")
                return failed + items[i:]
            batch = items[i:i + 30]
            futs = {ex.submit(call, t): t for t in batch}
            bad = []
            for f in as_completed(futs):
                try:
                    results[futs[f]] = f.result()
                except Exception:
                    bad.append(futs[f])
            if len(bad) > 0.6 * len(batch):
                strikes += 1
                if strikes > 4:
                    print(f"    {label}: Yahoo keeps blocking; stopping at {i}/{len(items)}, cached data used for the rest")
                    aborted = True
                    return failed + bad + items[i + len(batch):]
                wait = 120 * strikes
                print(f"    {label}: {len(bad)}/{len(batch)} failed, Yahoo is likely rate limiting; pausing {wait}s")
                time.sleep(wait)
                continue  # retry the same batch (its successes are already kept)
            strikes = max(0, strikes - 1)
            failed += bad
            i += len(batch)
            if i % 600 < 30:
                print(f"    {label}: {i}/{len(items)}  ({time.time() - t0:.0f}s, {len(failed)} failed so far)")
        return failed

    with ThreadPoolExecutor(WORKERS) as ex:
        failed = run(todo, ex)
        if failed and not aborted and time.time() < deadline:
            time.sleep(30)
            failed = run(failed, ex)
    failed = [t for t in failed if t not in results]
    return results, failed


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

    Pages through Yahoo's whole screener list for each country (it doesn't reliably sort by size, so we
    can't stop early). The size floor is applied loosely here and exactly once per-stock data is in.
    """
    import yfinance as yf

    EQ = yf.EquityQuery
    out: dict[str, dict] = {}
    floor = INTL_MIN_MCAP_USD * 0.7
    for region, exchanges in INTL_EXCHANGES.items():
        q = EQ("and", [EQ("eq", ["region", region]), EQ("is-in", ["exchange", *exchanges])])
        offset, n, seen, skipped = 0, 0, 0, 0
        for _ in range(MAX_SCREENER_PAGES):
            try:
                res = with_retries(lambda: yf.screen(q, offset=offset, size=250, sortField="intradaymarketcap", sortAsc=False))
            except Exception as e:
                print(f"  ! {region}: screener failed at offset {offset}: {type(e).__name__}", file=sys.stderr)
                break
            quotes = res.get("quotes") or []
            for qt in quotes:
                seen += 1
                cur, _ = base_ccy(qt.get("currency"))
                rate, mc = fx.get(cur), num(qt.get("marketCap"))
                if rate is None or mc is None or not qt.get("symbol") or qt.get("quoteType", "EQUITY") != "EQUITY":
                    continue
                # loose: treats minor units (pence) as major, so nothing is cut wrongly
                if mc * rate < floor:
                    continue
                home_ccy = CROSS_LISTING_VENUES.get(region)
                if home_ccy and qt.get("financialCurrency") and qt["financialCurrency"] != home_ccy:
                    skipped += 1
                    continue
                out[qt["symbol"]] = {"region": region, "qmc": mc * rate}
                n += 1
            offset += len(quotes)
            if not quotes or offset >= (res.get("total") or 0):
                break
            time.sleep(0.3)
        print(f"    {region}: {n} of {seen} listings" + (f" ({skipped} cross-listings skipped)" if skipped else ""))
    # Keep listings seen in the last 14 days, so one bad screener answer doesn't drop a whole country
    cache_file = CACHE / "universe_intl.json"
    cached = load_json(cache_file, {})
    today = dt.date.today().isoformat()
    for t, v in out.items():
        cached[t] = {**v, "seen": today}
    cutoff = (dt.date.today() - dt.timedelta(days=14)).isoformat()
    cached = {t: v for t, v in cached.items() if v.get("seen", today) >= cutoff and v.get("region") in INTL_EXCHANGES}
    save_json(cache_file, cached)
    return {t: {k: v[k] for k in ("region", "qmc") if k in v} for t, v in cached.items()}


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


def _fingerprint(r: dict):
    """Listings of the same company share Yahoo's fundamentals, so identical revenue in the same reporting
    currency identifies them even when the names differ ("BASF SE" vs "BASF SE NA O.N.")."""
    rev, ccy = r.get("_rev"), r.get("_fin")
    if rev and rev > 0 and ccy:
        return ("rev", ccy, sig(rev, 7))
    return ("name", norm_name(r.get("name")) or r["t"])


def dedupe(recs: dict[str, dict]) -> dict[str, dict]:
    """One listing per company.

    1. Drop foreign listings of companies whose home market is covered elsewhere (US stocks on Mexico's or
       Vienna's exchange), using the company's country of domicile.
    2. Among listings of the same company (same fundamentals; same name if Yahoo has no revenue), keep the best: US/extra first,
       then the home-country listing, then the one in the local currency, then the most traded.
    """
    keep = {}
    for t, r in recs.items():
        home = COUNTRY_MARKET.get(r.get("country"))
        if r.get("region") not in ("us", "extra") and home and _family(home) != _family(r.get("mkt")):
            continue
        keep[t] = r

    def rank(x):
        region = x.get("region")
        local = {"gb": ("GBp", "GBP"), "za": ("ZAc", "ZAR"), "il": ("ILA", "ILS")}.get(region)
        in_local = x.get("cur") in local if local else x.get("cur") != "USD" or region in ("us", "extra")
        return (region in ("us", "extra"), REGION_COUNTRY.get(region) == x.get("country"), in_local, x.get("_adv") or 0)

    best: dict = {}
    for t, r in keep.items():
        key = _fingerprint(r)
        if key not in best or rank(r) > rank(keep[best[key]]):
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
    inc = with_retries(lambda: t.income_stmt, tries=2)
    if inc is None or inc.empty:
        raise YahooRateLimitOrEmpty(ticker)  # don't cache an empty answer as if it were real
    bal = with_retries(lambda: t.balance_sheet, tries=2)
    cf = with_retries(lambda: t.cashflow, tries=2)
    return statement_metrics(inc, bal, cf)


# ---------------------------------------------------------------------------
# info (TTM snapshot), currency handling and prices
# ---------------------------------------------------------------------------

INFO_KEYS = ("quoteType", "shortName", "longName", "sector", "industry", "country", "currency", "financialCurrency",
             "currentPrice", "regularMarketPrice", "marketCap", "sharesOutstanding", "impliedSharesOutstanding",
             "trailingEps", "trailingPE", "trailingPegRatio", "pegRatio", "forwardPE", "priceToSalesTrailing12Months",
             "returnOnEquity", "grossMargins", "operatingMargins", "currentRatio", "revenueGrowth", "beta",
             "recommendationMean", "numberOfAnalystOpinions", "heldPercentInsiders", "heldPercentInstitutions",
             "shortPercentOfFloat", "trailingAnnualDividendYield", "debtToEquity", "freeCashflow", "totalRevenue",
             "totalDebt", "totalCash", "ebitda", "averageVolume", "targetMeanPrice", "fullExchangeName", "exchange")


def fetch_info(ticker: str) -> dict:
    import yfinance as yf

    def get():
        info = yf.Ticker(ticker).info or {}
        if not (info.get("shortName") or info.get("longName")) or not (
                info.get("marketCap") or info.get("regularMarketPrice") or info.get("currentPrice")):
            raise YahooRateLimitOrEmpty(ticker)
        return {k: info[k] for k in INFO_KEYS if info.get(k) is not None}

    return with_retries(get, tries=2)


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
    if from_shares is None and cur in MINOR_CCY:
        cands.reverse()  # Yahoo gives London market caps in pounds even though prices are in pence
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
    out["_rev"], out["_fin"] = rev, fin  # fingerprint for spotting the same company on several exchanges
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


def _download(tickers: list[str], period: str, chunk: int) -> pd.DataFrame:
    import yfinance as yf

    frames = []
    for i in range(0, len(tickers), chunk):
        part = tickers[i:i + chunk]
        try:
            df = with_retries(lambda: yf.download(part, period=period, interval="1d", auto_adjust=True,
                                                  progress=False, threads=True, group_by="column"))
        except Exception as e:
            print(f"    ! price chunk failed: {type(e).__name__}", file=sys.stderr)
            continue
        if df is None or df.empty:
            continue
        close = df["Close"] if isinstance(df.columns, pd.MultiIndex) else df[["Close"]].rename(columns={"Close": part[0]})
        frames.append(close.dropna(axis=1, how="all"))
        time.sleep(1)
    return pd.concat(frames, axis=1) if frames else pd.DataFrame()


def download_prices(tickers: list[str], period="14mo") -> pd.DataFrame:
    """Batched daily closes. Tickers that come back empty (often Yahoo throttling) get one slower retry."""
    closes = _download(tickers, period, 100)
    missing = [t for t in tickers if t not in closes.columns]
    if missing and len(missing) < len(tickers):
        print(f"    {len(missing)} price series missing; retrying slowly")
        time.sleep(60)
        more = _download(missing, period, 40)
        closes = pd.concat([closes, more], axis=1) if not more.empty else closes
    return closes.loc[:, ~closes.columns.duplicated()] if not closes.empty else closes

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
PRIVATE = ("_b", "_p", "_z", "_adv")
DROP = ("_rev", "_fin")                       # only needed during the run          # kept in state.json for the hourly job, not sent to the browser
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
    public = [{k: v for k, v in s.items() if k not in PRIVATE and k not in DETAIL and k not in DROP} for s in stocks]
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

PRICE_KEYS = ("price", "chg1d", "perf3m", "perf6m", "perf12m", "mom", "dma200", "off_high", "vol", "mdd")


def previous_records() -> tuple[dict, dict]:
    """Last published stocks (merged with their hourly state) and detail, used when Yahoo fails today."""
    screen = load_json(DATA / "screen.json", {})
    if screen.get("demo"):
        return {}, {}
    state = load_json(DATA / "state.json", {})
    prev = {s["t"]: {**s, **state.get(s["t"], {})} for s in screen.get("stocks", [])}
    return prev, load_json(DATA / "detail.json", {})


def run_full(limit: int | None = None) -> None:
    t0 = time.time()
    deadline = t0 + TIME_BUDGET_MIN * 60
    today = dt.date.today()
    prev, prev_detail = previous_records()

    print("1/6 currencies and universe")
    fx = fx_rates(BASE_CCYS if INTERNATIONAL else [])
    uni = us_universe()
    for t in EXTRA_TICKERS:
        uni.setdefault(t, {"region": "extra", "idx": "Extra"})
    if INTERNATIONAL:
        for t, v in intl_universe(fx).items():
            uni.setdefault(t, v)
    for t, r in prev.items():  # keep last run's stocks if a country list failed to load today
        if r.get("region") and (INTERNATIONAL or r["region"] in ("us", "extra")):
            uni.setdefault(t, {k: r[k] for k in ("region", "idx") if r.get(k)})
    tickers = sorted(uni)[:limit] if limit else sorted(uni)
    print(f"    {len(tickers)} listings")

    print("2/6 prices (batched)")
    closes = download_prices(tickers)
    print(f"    {closes.shape[1]} price series")

    print("3/6 TTM snapshots (Yahoo info, refreshed every few days)")
    info_cache = load_json(CACHE / "info.json.gz", {})

    def info_age(t):
        return (today - dt.date.fromisoformat(info_cache[t]["_at"])).days if t in info_cache else 10 ** 6

    def size(t):
        return (uni[t].get("qmc") or 0) if uni[t]["region"] not in ("us", "extra") else 1e15

    due = [t for t in tickers if info_age(t) >= INFO_MAX_AGE_DAYS]
    due.sort(key=lambda t: (t in info_cache, -info_age(t), -size(t)))  # never-fetched first, US first, biggest first
    print(f"    {len(due)} due, up to {INFO_BUDGET} this run, {len(info_cache)} cached")
    info_got, failed = fetch_many(fetch_info, due, "info", deadline, INFO_BUDGET)
    for t, info in info_got.items():
        info_cache[t] = {**info, "_at": today.isoformat()}
    save_json(CACHE / "info.json.gz", info_cache)
    fx = fx_rates({c for i in info_cache.values() for c in (i.get("currency"), i.get("financialCurrency"))} - {None}, fx)

    recs: dict[str, dict] = {}
    carried = 0
    for t in tickers:
        u = uni[t]
        info = info_cache.get(t)
        if info is None:
            if t in prev:  # not fetched yet / failed: keep the last published record, prices refresh below
                recs[t] = {k: v for k, v in prev[t].items() if k not in ("sub", "score", "coverage")}
                carried += 1
            continue
        if info.get("quoteType") not in (None, "EQUITY"):
            continue
        r = {"t": t, "idx": u.get("idx") or (info.get("fullExchangeName") or info.get("exchange")),
             "region": u["region"], **info_fields(info, fx), "info_at": info["_at"]}
        r["mkt"] = MARKET_OF_REGION.get(u["region"]) or COUNTRY_MARKET.get(r.get("country")) or "Other"
        recs[t] = r
    recs = {t: r for t, r in recs.items()
            if r.get("region") in ("us", "extra") or (r.get("mcap") or 0) >= INTL_MIN_MCAP_USD}
    before = len(recs)
    recs = dedupe(recs)
    print(f"    {len(recs)} stocks ({carried} carried from the last run), {before - len(recs)} duplicate/foreign listings removed")

    stale_px = 0
    for t, r in recs.items():
        if t in closes.columns and closes[t].dropna().size >= 30:
            r.update(price_metrics(closes[t]))
        elif t in prev:  # Yahoo didn't send prices today: keep yesterday's
            r.update({k: prev[t].get(k) for k in PRICE_KEYS if k in prev[t]})
            if prev[t].get("_p"):
                r["_p"] = prev[t]["_p"]
            pd_ = prev_detail.get(t) or {}
            r["_spark"], r["_dates"] = pd_.get("spark"), pd_.get("dates")
            stale_px += 1
        if r.get("_b") is not None:
            r["_b"]["mcap_px"] = r["_b"].get("mcap_px") or r.get("price")
    if stale_px:
        print(f"    {stale_px} stocks kept yesterday's prices")

    print("4/6 annual statements (cached weekly)")
    cache = load_json(CACHE / "fundamentals.json", {})
    cache = {t: v for t, v in cache.items() if v.get("fy_end")}  # drop empty answers saved by older versions

    def age(t):
        return (today - dt.date.fromisoformat(cache[t]["_at"])).days if t in cache else 10 ** 6

    eligible = [t for t, r in recs.items()
                if r.get("sector") not in EXCLUDE_SECTORS and (r.get("mcap") or 0) >= STATEMENT_MIN_MCAP
                and age(t) >= STATEMENT_MAX_AGE_DAYS]
    eligible.sort(key=lambda t: (t in cache, -age(t), -(recs[t].get("mcap") or 0)))  # missing first, then stalest
    print(f"    {len(eligible)} due, up to {STATEMENT_BUDGET} this run, {len(cache)} cached")
    got, _ = fetch_many(fetch_statements, eligible, "statements", deadline, STATEMENT_BUDGET)
    for t, m in got.items():
        if m.get("fy_end"):
            cache[t] = {**m, "_at": today.isoformat()}
    done = len(got)
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
        elif t in prev_detail and prev_detail[t].get("revh") and "revh" not in r:
            r["revh"] = prev_detail[t]["revh"]

    print("5/6 scoring")
    stocks = sorted(recs.values(), key=lambda r: r["t"])
    finalize(stocks)
    crit = criteria_spec()
    when = now_utc()

    print("6/6 writing")
    detail = {s["t"]: {"spark": s.get("_spark"), "dates": s.get("_dates"), "revh": s.get("revh")} for s in stocks}
    history = update_history(stocks, crit, when)
    n_intl = sum(s["region"] not in ("us", "extra") for s in stocks)
    n_us = len(stocks) - n_intl
    write_outputs({
        "generated": when, "prices_updated": when, "demo": False,
        "universe": f"{n_us:,} US" + (f" + {n_intl:,} international" if n_intl else ""),
        "stats": {"listings": len(tickers), "loaded": len(stocks), "info_pulled": len(info_got),
                  "info_failed": len(failed), "carried_from_last_run": carried, "stale_prices": stale_px,
                  "statements_cached": len(cache), "statements_pulled": done},
    }, stocks, detail)
    save_json(DATA / "history.json", history)
    n = sum(passes(s, crit) for s in stocks)
    print(f"done in {time.time() - t0:.0f}s: {len(stocks)} stocks, {n} pass the default screen, "
          f"{len(failed)} snapshots failed, {stale_px} on yesterday's prices")
    pending = sum(1 for t in tickers if t not in info_cache)
    if pending:
        print(f"note: {pending} listings still waiting for their first snapshot; they fill in over the next runs")


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
