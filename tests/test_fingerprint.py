"""Fingerprints: same business should look alike, and missing data must not fake it."""

from __future__ import annotations

import numpy as np
import pandas as pd

from sentinel.analytics import fingerprint


def make_panel():
    base = {
        "fiscal_year": 2024, "assets": 1e9, "revenue": 5e8, "net_income": 5e7,
        "gross_profit": 2e8, "operating_income": 8e7, "equity": 4e8, "liabilities": 6e8,
        "cfo": 6e7, "capex": 2e7, "rd_expense": 1e7, "inventory": 5e7,
        "receivables": 4e7, "sbc": 1e7, "dividends_paid": 1e7, "interest_expense": 5e6,
        "tax_expense": 1e7, "pretax_income": 7e7,
    }
    rows = []
    # two brewers, one bank
    rows.append(dict(base, cik=1, sic="2080", bucket="manufacturing"))
    rows.append(dict(base, cik=2, sic="2080", bucket="manufacturing", net_income=4.6e7))
    rows.append(dict(base, cik=3, sic="6021", bucket="banks", leverage=0.9,
                     gross_profit=np.nan, inventory=np.nan, operating_income=3e7))
    frame = pd.DataFrame(rows)
    frame["log_assets"] = np.log10(frame["assets"])
    frame["log_revenue"] = np.log10(frame["revenue"])
    frame["gross_margin"] = frame["gross_profit"] / frame["revenue"]
    frame["operating_margin"] = frame["operating_income"] / frame["revenue"]
    frame["net_margin"] = frame["net_income"] / frame["revenue"]
    frame["roa"] = frame["net_income"] / frame["assets"]
    frame["roe"] = frame["net_income"] / frame["equity"]
    frame["leverage"] = frame["liabilities"] / frame["assets"]
    frame["asset_turnover"] = frame["revenue"] / frame["assets"]
    frame["cash_conversion"] = frame["cfo"] / frame["net_income"]
    frame["accruals"] = (frame["net_income"] - frame["cfo"]) / frame["assets"]
    frame["fcf_margin"] = (frame["cfo"] - frame["capex"]) / frame["revenue"]
    frame["capex_intensity"] = frame["capex"] / frame["revenue"]
    frame["rd_intensity"] = frame["rd_expense"] / frame["revenue"]
    frame["sbc_pct_revenue"] = frame["sbc"] / frame["revenue"]
    frame["inventory_intensity"] = frame["inventory"] / frame["assets"]
    frame["receivables_intensity"] = frame["receivables"] / frame["assets"]
    frame["dividend_payout"] = frame["dividends_paid"] / frame["net_income"]
    frame["interest_coverage"] = frame["operating_income"] / frame["interest_expense"]
    frame["revenue_yoy"] = 0.05
    frame["sic4"] = frame["sic"].str[:4]
    return frame


def test_same_business_ranks_first():
    frame = make_panel()
    result = fingerprint.peers(None, 1, k=2, frame=frame)
    assert len(result) == 2
    assert int(result.iloc[0]["cik"]) == 2, "the other brewer must outrank the bank"
    assert result.iloc[0]["similarity"] > result.iloc[1]["similarity"]


def test_sparse_vectors_are_excluded():
    frame = make_panel()
    sparse = frame.iloc[[0]].copy()
    sparse["cik"] = 9
    for col in fingerprint.FEATURES:
        if col not in ("log_assets", "log_revenue"):
            sparse[col] = np.nan
    combined = pd.concat([frame, sparse], ignore_index=True)
    result = fingerprint.peers(None, 1, k=5, frame=combined)
    assert 9 not in set(int(x) for x in result["cik"])
