"""Offline tests: synthetic Yahoo-shaped statements and prices, no network."""
import json
import math
import sys
from pathlib import Path

import numpy as np
import pandas as pd
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import screener as S  # noqa: E402

FX = {"USD": 1.0, "JPY": 0.0067, "GBP": 1.30, "GBp": 0.013, "TWD": 0.031, "EUR": 1.08}
DATES = pd.to_datetime(["2025-12-31", "2024-12-31", "2023-12-31", "2022-12-31"])


def frame(rows):
    return pd.DataFrame(rows, index=DATES).T


@pytest.fixture
def statements():
    inc = frame({
        "Total Revenue": [1331, 1210, 1100, 1000],
        "Gross Profit": [800, 710, 640, 560],
        "EBIT": [300, 260, 230, 200],
        "Tax Provision": [60, 52, 46, 40],
        "Pretax Income": [290, 250, 220, 190],
        "Net Income": [230, 198, 174, 150],
        "Diluted EPS": [2.30, 1.98, 1.74, 1.50],
        "Diluted Average Shares": [100, 100, 101, 102],
        "Interest Expense": [10, 10, 10, 10],
    })
    bal = frame({
        "Total Debt": [200, 200, 220, 240],
        "Stockholders Equity": [1000, 900, 820, 750],
        "Cash Cash Equivalents And Short Term Investments": [200, 150, 120, 100],
        "Total Assets": [1600, 1500, 1420, 1350],
        "Current Assets": [700, 620, 560, 500],
        "Current Liabilities": [350, 330, 320, 310],
        "Long Term Debt": [180, 185, 200, 220],
        "Total Liabilities Net Minority Interest": [600, 600, 600, 600],
        "Retained Earnings": [800, 650, 520, 400],
    })
    cf = frame({"Operating Cash Flow": [320, 280, 240, 210], "Free Cash Flow": [260, 220, 190, 160]})
    return inc, bal, cf


def test_statement_metrics(statements):
    m = S.statement_metrics(*statements)
    assert m["rev3y"] == pytest.approx(0.10, abs=0.002)          # 1000 -> 1331 over 3y
    assert m["eps3y"] == pytest.approx((2.30 / 1.50) ** (1 / 3) - 1, abs=0.002)
    assert m["rev_up"] == 3
    # ROIC latest: 300 * (1 - 60/290) / (200 + 1000 - 200)
    assert m["roic"] == pytest.approx(300 * (1 - 60 / 290) / 1000, abs=1e-3)
    assert m["roic_avg"] is not None and m["roic_avg"] < m["roic"] + 0.05
    assert m["gm_min"] == pytest.approx(0.56, abs=1e-3)
    assert m["fcf_years"] == 4
    assert m["int_cov"] == pytest.approx(30)
    assert m["dilution"] < 0                                       # buybacks
    assert m["fscore"] == 9
    assert m["fy_end"] == "2025-12-31"
    assert [y for y, _ in m["revh"]] == [2022, 2023, 2024, 2025]


def test_statement_metrics_handles_missing_and_negative():
    inc = frame({"Total Revenue": [100, 120, np.nan, 90], "Diluted EPS": [-1.0, 0.5, 0.4, 0.3], "EBIT": [-5, 4, 3, 2]})
    m = S.statement_metrics(inc, None, None)
    assert m["eps3y"] is None                                      # negative end point
    assert m["roic"] is None                                       # no balance sheet
    assert "fscore" not in m
    assert S.statement_metrics(None, None, None)["fy_end"] is None


def test_price_metrics_and_momentum():
    idx = pd.bdate_range(end="2026-10-23", periods=300)
    close = pd.Series(np.linspace(50, 100, 300), index=idx)
    m = S.price_metrics(close)
    assert m["price"] == pytest.approx(100)
    assert m["mom"] == pytest.approx(close.iloc[-22] / close.iloc[-253] - 1)
    assert m["off_high"] == pytest.approx(0)
    assert m["mdd"] == pytest.approx(0)
    assert m["dma200"] > 0
    assert len(m["_spark"]) <= 53


def base_record(**kw):
    info = {"marketCap": 50e9, "currentPrice": 100, "trailingEps": 5, "trailingPE": 20, "trailingPegRatio": 1.6,
            "freeCashflow": 3e9, "totalRevenue": 20e9, "debtToEquity": 45.0, "grossMargins": 0.6,
            "ebitda": 5e9, "totalDebt": 4e9, "totalCash": 6e9, "enterpriseValue": 48e9, "targetMeanPrice": 120,
            "sector": "Technology", "forwardPE": 18}
    info.update(kw)
    return {"t": "TEST", **S.info_fields(info, FX)}


def test_info_and_derive():
    r = base_record()
    assert r["de"] == pytest.approx(0.45)                          # percent -> ratio
    assert r["nd_ebitda"] == pytest.approx(-0.4)                   # net cash
    S.derive(r)
    assert r["pe"] == pytest.approx(20)
    assert r["peg"] == pytest.approx(1.6)
    assert r["fcf_yield"] == pytest.approx(0.06)
    assert r["upside"] == pytest.approx(0.2)
    assert r["ev_ebitda"] == pytest.approx((50 - 2) / 5)
    # hourly: price moves 10% -> price ratios follow
    r["price"] = 110
    S.derive(r)
    assert r["mcap"] == pytest.approx(55e9)
    assert r["pe"] == pytest.approx(22)
    assert r["peg"] == pytest.approx(1.76)
    assert r["fpe"] == pytest.approx(18 * 1.1)


def test_screen_logic():
    crit = S.criteria_spec()
    good = {"sector": "Technology", "mcap": 5e9, "pe": 25, "peg": None, "rev3y": 0.12, "roic": 0.2,
            "fcfm": 0.15, "de": 0.5, "gm": 0.5, "mom": 0.1}
    assert S.passes(good, crit)
    assert not S.passes({**good, "sector": "Financial Services"}, crit)
    assert not S.passes({**good, "de": 3.2}, crit)
    assert not S.passes({**good, "roic": None}, crit)               # missing data fails
    assert S.passes({**good, "pe": 45, "peg": 1.5}, crit)            # PEG rescues a high P/E
    assert not S.passes({**good, "pe": -10, "peg": None}, crit)      # loss makers fail
    by_key = {c["key"]: c for c in crit}
    assert S.check({"fscore": 7}, by_key["fscore"]) is True
    assert S.check({}, by_key["fscore"]) is None


def test_finalize_scores_and_rounds():
    stocks = []
    for i in range(20):
        r = base_record(trailingEps=2 + i * 0.2)
        r.update(t=f"T{i}", roic=0.05 + i * 0.01, rev3y=0.02 * i, mom=-0.1 + 0.02 * i, fscore=i % 10)
        stocks.append(r)
    S.finalize(stocks)
    scores = [s["score"] for s in stocks]
    assert all(0 <= x <= 100 for x in scores)
    assert scores[-1] > scores[0]
    assert all(set(s["sub"]) == {"quality", "growth", "value", "momentum"} for s in stocks)
    assert not any(isinstance(v, float) and not math.isfinite(v) for s in stocks for v in s.values())


def test_foreign_listing_converts_to_usd():
    # Japanese stock: price in yen, statements in yen
    r = base_record(currency="JPY", financialCurrency="JPY", currentPrice=3000, marketCap=3e12, sharesOutstanding=1e9,
                    trailingEps=150, trailingPE=20, freeCashflow=2e11, totalRevenue=2e12, ebitda=4e11,
                    totalDebt=1e11, totalCash=3e11, priceToSalesTrailing12Months=1.5, targetMeanPrice=3300)
    S.derive(r)
    assert r["mcap"] == pytest.approx(3e12 * 0.0067)
    assert r["pe"] == pytest.approx(20)
    assert r["fcf_yield"] == pytest.approx(2e11 / 3e12)
    assert r["ps"] == pytest.approx(1.5)
    assert r["upside"] == pytest.approx(0.1)


def test_pence_listing():
    # London: price in pence, market cap and statements in pounds
    r = base_record(currency="GBp", financialCurrency="GBP", currentPrice=2500, marketCap=25e9, sharesOutstanding=1e9,
                    trailingEps=1.25, trailingPE=20, freeCashflow=1.5e9, totalRevenue=10e9,
                    priceToSalesTrailing12Months=2.5)
    S.derive(r)
    assert r["mcap"] == pytest.approx(25e9 * 1.30, rel=0.01)   # not 100x too small
    assert r["pe"] == pytest.approx(20, rel=0.01)                # EPS converted to pence
    assert r["fcf_yield"] == pytest.approx(0.06, rel=0.01)


def test_adr_with_foreign_statements():
    # ADR priced in USD, Yahoo EPS already per ADR in USD, revenue in TWD
    r = base_record(currency="USD", financialCurrency="TWD", currentPrice=200, marketCap=1e12, sharesOutstanding=5e9,
                    trailingEps=8, trailingPE=25, freeCashflow=1e12, totalRevenue=3e12,
                    priceToSalesTrailing12Months=1e12 / (3e12 * 0.031))
    S.derive(r)
    assert r["pe"] == pytest.approx(25)                          # EPS left alone (P/E cross-check)
    assert r["ps"] == pytest.approx(1e12 / (3e12 * 0.031), rel=0.01)
    assert r["fcf_yield"] == pytest.approx(1e12 * 0.031 / 1e12, rel=0.01)


def test_missing_fx_means_no_mcap():
    r = base_record(currency="XYZ", financialCurrency="XYZ")
    assert r["mcap"] is None


def test_dedupe():
    def r(name, country, region, mkt, rev=None, fin="EUR", cur="EUR", adv=0):
        return {"name": name, "country": country, "region": region, "mkt": mkt, "_rev": rev, "_fin": fin, "cur": cur, "_adv": adv}

    recs = {
        # same company, different names per exchange: matched by identical fundamentals, home listing wins
        "SAP.DE": r("SAP SE", "Germany", "de", "Europe", 34.2e9, adv=5e8),
        "SAP.VI": r("SAP SE INHABER-AKTIEN O.N.", "Germany", "at", "Europe", 34.2e9, adv=9e6),
        # London: pence line beats the USD ADR line even with less dollar volume
        "DGE.L": r("Diageo plc", "United Kingdom", "gb", "UK & Ireland", 20.2e9, "USD", "GBp", adv=1e8),
        "DGED.L": r("DIAGEO PLC ADR (4:1)", "United Kingdom", "gb", "UK & Ireland", 20.2e9, "USD", "USD", adv=2e8),
        # Dutch company listed in Paris and Frankfurt (no home listing): most traded wins
        "AIR.PA": r("Airbus SE", "Netherlands", "fr", "Europe", 69e9, adv=4e8),
        "AIR.DE": r("Airbus SE", "Netherlands", "de", "Europe", 69e9, adv=5e7),
        # US companies: S&P listing wins; non-S&P US company on Mexico's exchange is dropped
        "AAPL": r("Apple Inc.", "United States", "us", "US", 400e9, "USD", "USD"),
        "AAPL.MX": r("Apple Inc.", "United States", "mx", "Latin America", 400e9, "USD", "MXN", adv=1e9),
        "XYZ.MX": r("Some US Co", "United States", "mx", "Latin America", 3e9, "USD", "MXN"),
        # share classes of one company collapse to the most traded
        "005930.KS": r("Samsung Electronics", "South Korea", "kr", "Asia-Pacific", 300e12, "KRW", "KRW", adv=9e8),
        "005935.KS": r("Samsung Electronics Pref", "South Korea", "kr", "Asia-Pacific", 300e12, "KRW", "KRW", adv=1e8),
        # different companies with similar names stay separate
        "DAL": r("Delta Air Lines", "United States", "us", "US", 61e9, "USD", "USD"),
        "DELTA.BK": r("Delta Electronics (Thailand)", "Thailand", "th", "Asia-Pacific", 150e9, "THB", "THB"),
        # no revenue from Yahoo: falls back to the name
        "SHEL.L": r("Shell plc", "United Kingdom", "gb", "UK & Ireland", None, "USD", "GBp", adv=5e8),
        "SHELL.AS": r("Shell PLC", "United Kingdom", "nl", "Europe", None, "USD", "EUR", adv=2e8),
    }
    out = S.dedupe(recs)
    assert set(out) == {"SAP.DE", "DGE.L", "AIR.PA", "AAPL", "005930.KS", "DAL", "DELTA.BK", "SHEL.L"}


def test_intl_universe_reads_every_page_even_unsorted(monkeypatch):
    """Yahoo doesn't reliably sort by size, so big companies can sit on any page."""
    import random
    import yfinance as yf

    rnd_ = random.Random(1)
    listings = [{"symbol": f"{i}.T", "marketCap": 1e13 / (i + 1), "currency": "JPY", "quoteType": "EQUITY"} for i in range(3000)]
    rnd_.shuffle(listings)
    xetra = [{"symbol": "SAP.DE", "marketCap": 2e11, "currency": "EUR", "financialCurrency": "EUR"},
             {"symbol": "APC.DE", "marketCap": 3e12, "currency": "EUR", "financialCurrency": "USD"}]  # Apple on XETRA

    def fake_screen(q, offset=0, size=250, sortField=None, sortAsc=None):
        region = q.to_dict()["operands"][0]["operands"][1]
        data = {"jp": listings, "de": xetra}.get(region, [])
        return {"total": len(data), "quotes": data[offset:offset + size]}

    monkeypatch.setattr(yf, "screen", fake_screen)
    monkeypatch.setattr(S, "INTL_EXCHANGES", {"jp": ["JPX"], "de": ["GER"]})
    monkeypatch.setattr(S.time, "sleep", lambda s: None)
    out = S.intl_universe({"USD": 1, "JPY": 0.0067, "EUR": 1.1})
    floor = S.INTL_MIN_MCAP_USD * 0.7
    expected = {f"{i}.T" for i in range(3000) if 1e13 / (i + 1) * 0.0067 >= floor}
    assert {t for t in out if t.endswith(".T")} == expected
    assert "0.T" in out                                   # the biggest company is found wherever it sits
    assert "SAP.DE" in out and "APC.DE" not in out        # cross-listing skipped on XETRA


def test_pence_market_cap_without_share_count():
    r = base_record(currency="GBp", financialCurrency="GBP", currentPrice=2500, marketCap=25e9, sharesOutstanding=None,
                    impliedSharesOutstanding=None, trailingEps=1.25, trailingPE=20)
    assert r["mcap"] == pytest.approx(25e9 * 1.30)


def test_norm_name():
    assert S.norm_name("Shell plc") == S.norm_name("SHELL PLC") == "shell"
    assert S.norm_name("Toyota Motor Corporation") == "toyota motor"


def test_fetch_info_rejects_empty_answers(monkeypatch):
    import yfinance as yf

    class T:
        def __init__(self, t):
            self.info = {"trailingPegRatio": None} if t == "BLOCKED" else {"shortName": "Ok Co", "marketCap": 5e9, "sector": "Technology"}

    monkeypatch.setattr(yf, "Ticker", T)
    monkeypatch.setattr(S.time, "sleep", lambda s: None)
    assert S.fetch_info("OK")["shortName"] == "Ok Co"
    with pytest.raises(S.YahooRateLimitOrEmpty):
        S.fetch_info("BLOCKED")


def test_fetch_many_backs_off_and_retries_when_blocked(monkeypatch):
    sleeps = []
    monkeypatch.setattr(S.time, "sleep", lambda s: sleeps.append(s))
    monkeypatch.setattr(S, "REQUEST_PAUSE", 0)
    state = {"blocked_calls": 40}

    def fn(t):
        if state["blocked_calls"] > 0:  # Yahoo blocks the first 40 calls, then recovers
            state["blocked_calls"] -= 1
            raise S.YahooRateLimitOrEmpty(t)
        return t.lower()

    tickers = [f"T{i}" for i in range(100)]
    got, failed = fn_res = S.fetch_many(fn, tickers, "test", deadline=S.time.time() + 3600)
    assert len(got) == 100 and not failed          # blocked batches were retried, nothing lost
    assert any(s >= 120 for s in sleeps)           # it paused when blocked


def test_fetch_many_gives_up_after_repeated_blocks(monkeypatch):
    monkeypatch.setattr(S.time, "sleep", lambda s: None)
    monkeypatch.setattr(S, "REQUEST_PAUSE", 0)

    def fn(t):
        raise S.YahooRateLimitOrEmpty(t)

    got, failed = S.fetch_many(fn, [f"T{i}" for i in range(90)], "test", deadline=S.time.time() + 3600)
    assert not got and len(failed) == 90


def test_full_run_survives_yahoo_blocking(tmp_path, monkeypatch):
    """Second run: Yahoo blocks snapshots and prices for half the stocks. Nothing should go blank."""
    sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "tools"))
    import importlib
    demo = importlib.import_module("make_demo")
    demo.main(tmp_path)                                  # first run (sample data), then mark it as real
    screen = json.loads((tmp_path / "screen.json").read_text())
    screen["demo"] = False
    S.save_json(tmp_path / "screen.json", screen, records=True)
    first = {s["t"]: s for s in screen["stocks"]}

    S.DATA, S.CACHE = tmp_path, tmp_path / "cache"
    blocked = set(sorted(first)[len(first) // 2:])
    real_info, real_prices = S.fetch_info, S.download_prices

    def info(t):
        if t in blocked:
            raise S.YahooRateLimitOrEmpty(t)
        return real_info(t)

    monkeypatch.setattr(S, "fetch_info", info)
    monkeypatch.setattr(S, "download_prices", lambda tk, period="14mo": real_prices([t for t in tk if t not in blocked], period))
    monkeypatch.setattr(S.time, "sleep", lambda s: None)
    S.run_full()
    second = {s["t"]: s for s in json.loads((tmp_path / "screen.json").read_text())["stocks"]}
    for t in blocked & set(first):
        assert second[t]["name"] == first[t]["name"]
        assert second[t]["price"] == first[t]["price"]
        assert second[t].get("mcap") is not None or first[t].get("mcap") is None
