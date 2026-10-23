#!/usr/bin/env python3
"""Generate SAMPLE data for fictional companies by running the real pipeline on synthetic inputs.

Used to preview the dashboard before the first real run, and as an end-to-end test.
    python tools/make_demo.py [out_dir]     # default: docs/data
The output is flagged "demo": true and the dashboard shows a banner. The first real run overwrites it.
"""
import json
import random
import sys
from pathlib import Path

import numpy as np
import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
import screener as S  # noqa: E402

rng = random.Random(7)
np_rng = np.random.default_rng(7)

A = ["North", "Blue", "Iron", "Cedar", "Summit", "Harbor", "Granite", "Silver", "Prairie", "Atlas", "Beacon", "Keystone",
     "Pioneer", "Meridian", "Orchard", "Falcon", "Copper", "Lakeside", "Redwood", "Polar", "Vantage", "Crescent", "Juniper"]
B = ["Instruments", "Systems", "Foods", "Logistics", "Software", "Therapeutics", "Industries", "Materials", "Networks",
     "Brands", "Energy", "Devices", "Analytics", "Health", "Robotics", "Semiconductor", "Outfitters", "Holdings"]
SECTORS = {"Technology": (0.6, 0.15), "Healthcare": (0.55, 0.08), "Industrials": (0.33, 0.06), "Consumer Cyclical": (0.4, 0.06),
           "Consumer Defensive": (0.38, 0.04), "Communication Services": (0.5, 0.07), "Energy": (0.3, 0.03),
           "Basic Materials": (0.28, 0.03), "Utilities": (0.4, 0.02), "Financial Services": (0.0, 0.05),
           "Real Estate": (0.6, 0.03)}
N = 320


def make_universe():
    names, tickers = set(), {}
    while len(tickers) < N:
        name = f"{rng.choice(A)} {rng.choice(B)}"
        if name in names:
            continue
        t = ("".join(w[0] for w in name.split()) + "".join(rng.choice("ABCDEFGHKLMNPRSTVWXZ") for _ in range(2)))[:4]
        if t in tickers:
            continue
        names.add(name)
        tickers[t] = {"name": name, "sector": rng.choices(list(SECTORS), weights=[16, 12, 14, 10, 6, 5, 5, 5, 4, 14, 6])[0],
                      "idx": rng.choice(["S&P 500", "S&P 400", "S&P 600"])}
    return tickers


UNI = make_universe()
WORLD = {}
for t, u in UNI.items():
    gm0, g0 = SECTORS[u["sector"]]
    gm = max(0.08, min(0.9, rng.gauss(gm0, 0.12)))
    growth = rng.gauss(g0, 0.08)
    rev0 = 10 ** rng.uniform(8.7, 11.3)
    om = max(-0.1, min(0.5, gm * rng.uniform(0.15, 0.6) + rng.gauss(0, 0.04)))
    WORLD[t] = dict(gm=gm, growth=growth, rev=rev0, om=om, lev=max(0.0, rng.gauss(0.7, 0.6)),
                    mult=max(4, rng.gauss(20, 8)), drift=rng.gauss(0.08, 0.25), vol=rng.uniform(0.18, 0.55))
    eps = rev0 * om * 0.76 / 1e8
    WORLD[t]["price"] = max(5.0, eps * WORLD[t]["mult"]) if eps > 0 else rng.uniform(8, 60)


def fake_statements(t):
    w = WORLD[t]
    dates = pd.to_datetime(["2025-12-31", "2024-12-31", "2023-12-31", "2022-12-31"])
    rev = [w["rev"] / (1 + w["growth"] + rng.gauss(0, 0.03)) ** i for i in range(4)]
    gm = [w["gm"] + rng.gauss(0, 0.015) for _ in range(4)]
    ebit = [r * (w["om"] + rng.gauss(0, 0.02)) for r in rev]
    equity = [r * rng.uniform(0.5, 1.2) for r in rev]
    debt = [e * w["lev"] for e in equity]
    cash = [r * rng.uniform(0.03, 0.25) for r in rev]
    assets = [e + d + r * 0.3 for e, d, r in zip(equity, debt, rev)]
    shares = [1e8 * (1 + rng.gauss(0.0, 0.015)) ** i for i in range(4)]
    ni = [e * 0.76 - d * 0.04 for e, d in zip(ebit, debt)]
    ocf = [n * rng.uniform(1.0, 1.4) for n in ni]
    inc = pd.DataFrame({"Total Revenue": rev, "Gross Profit": [r * g for r, g in zip(rev, gm)], "EBIT": ebit,
                        "Tax Provision": [e * 0.21 for e in ebit], "Pretax Income": ebit, "Net Income": ni,
                        "Diluted EPS": [n / s for n, s in zip(ni, shares)], "Diluted Average Shares": shares,
                        "Interest Expense": [d * 0.045 for d in debt]}, index=dates).T
    bal = pd.DataFrame({"Total Debt": debt, "Stockholders Equity": equity, "Cash And Cash Equivalents": cash,
                        "Total Assets": assets, "Current Assets": [a * 0.4 for a in assets],
                        "Current Liabilities": [a * rng.uniform(0.18, 0.4) for a in assets], "Long Term Debt": debt,
                        "Total Liabilities Net Minority Interest": [a - e for a, e in zip(assets, equity)],
                        "Retained Earnings": [e * 0.6 for e in equity]}, index=dates).T
    cf = pd.DataFrame({"Operating Cash Flow": ocf, "Free Cash Flow": [o - r * 0.04 for o, r in zip(ocf, rev)]}, index=dates).T
    return S.statement_metrics(inc, bal, cf)


PRICES = {}
idx = pd.bdate_range(end="2026-10-23", periods=300)
for t, w in WORLD.items():
    rets = np_rng.normal(w["drift"] / 252, w["vol"] / np.sqrt(252), len(idx))
    path = np.exp(np.cumsum(rets))
    PRICES[t] = pd.Series(path / path[-1] * w["price"], index=idx)


def fake_info(t):
    w, u = WORLD[t], UNI[t]
    ni = w["rev"] * w["om"] * 0.76
    shares = 1e8
    eps = ni / shares
    price = w["price"]
    mcap = price * shares
    equity = w["rev"] * 0.8
    fcf = ni * rng.uniform(0.6, 1.3)
    return {"quoteType": "EQUITY", "shortName": u["name"], "sector": u["sector"], "industry": u["sector"],
            "marketCap": mcap, "currentPrice": price, "trailingEps": eps, "trailingPE": price / eps if eps > 0 else None,
            "trailingPegRatio": (price / eps) / max(1, w["growth"] * 100 + rng.gauss(4, 3)) if eps > 0 else None,
            "forwardPE": price / (eps * (1 + w["growth"])) if eps > 0 else None,
            "freeCashflow": fcf, "totalRevenue": w["rev"], "debtToEquity": w["lev"] * 100,
            "grossMargins": w["gm"], "operatingMargins": w["om"], "returnOnEquity": ni / equity,
            "ebitda": w["rev"] * (w["om"] + 0.04), "totalDebt": equity * w["lev"], "totalCash": w["rev"] * 0.1,
            "enterpriseValue": mcap + equity * w["lev"] - w["rev"] * 0.1, "priceToSalesTrailing12Months": mcap / w["rev"],
            "currentRatio": rng.uniform(0.7, 3.0), "revenueGrowth": w["growth"] + rng.gauss(0, 0.04),
            "beta": rng.uniform(0.5, 1.8), "recommendationMean": rng.uniform(1.5, 3.4),
            "numberOfAnalystOpinions": rng.randint(3, 40), "heldPercentInsiders": rng.uniform(0.001, 0.15),
            "heldPercentInstitutions": rng.uniform(0.5, 0.98), "shortPercentOfFloat": rng.uniform(0.01, 0.15),
            "trailingAnnualDividendYield": rng.choice([0, 0, rng.uniform(0.005, 0.04)]),
            "targetMeanPrice": price * rng.uniform(0.85, 1.35)}


def main(out=ROOT / "docs" / "data"):
    out = Path(out)
    S.DATA, S.CACHE = out, out / "_demo_cache"
    S.get_universe = lambda: {t: u["idx"] for t, u in UNI.items()}
    S.fetch_info = fake_info
    S.fetch_statements = fake_statements
    S.download_prices = lambda tickers, period="14mo": pd.DataFrame({t: PRICES[t] for t in tickers})
    for f in ("history.json",):
        (out / f).unlink(missing_ok=True)
    S.run_full()

    screen = json.loads((out / "screen.json").read_text())
    screen.update(demo=True, generated="2026-10-23T21:45:00+00:00", prices_updated="2026-10-23T20:30:00+00:00",
                  universe="Sample universe (fictional companies)")
    S.save_json(out / "screen.json", screen, records=True)

    # fabricate 90 trading days of history so the trend chart has something to show
    crit = S.criteria_spec()
    passing = sorted(s["t"] for s in screen["stocks"] if S.passes(s, crit))
    near = [s["t"] for s in screen["stocks"] if s["t"] not in passing][:60]
    days, cur = [], set(passing)
    for d in reversed(pd.bdate_range(end="2026-10-23", periods=90)):
        days.append({"date": d.date().isoformat(), "n": len(cur), "passes": sorted(cur)})
        if cur and rng.random() < 0.5:
            cur.discard(rng.choice(sorted(cur)))
        if rng.random() < 0.45:
            cur.add(rng.choice(near))
    S.save_json(out / "history.json", {"days": list(reversed(days))})
    for p in S.CACHE.glob("*"):
        p.unlink()
    S.CACHE.rmdir()
    print(f"sample data written to {out}")


if __name__ == "__main__":
    main(*sys.argv[1:])
