"""Offline tests: synthetic Yahoo-shaped statements and prices, no network."""
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
    recs = {
        "SHEL.L": {"name": "Shell plc", "country": "United Kingdom", "region": "gb", "mkt": "UK & Ireland", "_adv": 5e8},
        "SHELL.AS": {"name": "Shell PLC", "country": "United Kingdom", "region": "nl", "mkt": "Europe", "_adv": 2e8},
        "AAPL": {"name": "Apple Inc.", "country": "United States", "region": "us", "mkt": "US"},
        "AAPL.MX": {"name": "Apple Inc.", "country": "United States", "region": "mx", "mkt": "Latin America"},
        "XYZ.MX": {"name": "Some US Co", "country": "United States", "region": "mx", "mkt": "Latin America"},
        "0700.HK": {"name": "Tencent Holdings Ltd", "country": "China", "region": "hk", "mkt": "China & HK"},
        "STLAM.MI": {"name": "Stellantis N.V.", "country": "Netherlands", "region": "it", "mkt": "Europe"},
        "LIN": {"name": "Linde plc", "country": "Ireland", "region": "us", "mkt": "US"},
    }
    out = S.dedupe(recs)
    assert set(out) == {"SHEL.L", "AAPL", "0700.HK", "STLAM.MI", "LIN"}


def test_intl_universe_pages_until_below_floor(monkeypatch):
    import yfinance as yf

    calls = []

    def fake_screen(q, offset=0, size=250, sortField=None, sortAsc=None):
        region = q.to_dict()["operands"][0]["operands"][1]
        calls.append((region, offset))
        if region != "jp":
            return {"quotes": [], "total": 0}
        caps = [1e13 / (i + 1) for i in range(offset, offset + 250)]  # yen, descending
        return {"total": 4000, "quotes": [{"symbol": f"{i}.T", "marketCap": c, "currency": "JPY", "quoteType": "EQUITY"}
                                          for i, c in zip(range(offset, offset + 250), caps)]}

    monkeypatch.setattr(yf, "screen", fake_screen)
    monkeypatch.setattr(S, "INTL_EXCHANGES", {"jp": ["JPX"], "de": ["GER"]})
    out = S.intl_universe({"USD": 1, "JPY": 0.0067})
    floor = S.INTL_MIN_MCAP_USD * 0.5
    expected = sum(1 for i in range(4000) if 1e13 / (i + 1) * 0.0067 >= floor)
    assert len(out) == expected and all(v["region"] == "jp" for v in out.values())
    assert len([c for c in calls if c[0] == "jp"]) < 16   # stopped paging early


def test_norm_name():
    assert S.norm_name("Shell plc") == S.norm_name("SHELL PLC") == "shell"
    assert S.norm_name("Toyota Motor Corporation") == "toyota motor"
