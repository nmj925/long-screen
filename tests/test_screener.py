"""Offline tests: synthetic Yahoo-shaped statements and prices, no network."""
import math
import sys
from pathlib import Path

import numpy as np
import pandas as pd
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import screener as S  # noqa: E402

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
    return {"t": "TEST", **S.info_fields(info)}


def test_info_and_derive():
    r = base_record()
    assert r["de"] == pytest.approx(0.45)                          # percent -> ratio
    assert r["nd_ebitda"] == pytest.approx(-0.4)                   # net cash
    S.derive(r)
    assert r["pe"] == pytest.approx(20)
    assert r["peg"] == pytest.approx(1.6)
    assert r["fcf_yield"] == pytest.approx(0.06)
    assert r["upside"] == pytest.approx(0.2)
    assert r["ev_ebitda"] == pytest.approx(48 / 5)
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
