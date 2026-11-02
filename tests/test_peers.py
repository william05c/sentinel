"""Peer matching: shape of the answer, not just that it returns something.

peers() shipped once with precision@10 of 0.0 and no test would have caught it. These
are the properties that make a peer list meaningful at all.
"""

from __future__ import annotations

import numpy as np
import pandas as pd

from sentinel.analytics import fingerprint


def panel(n_same=4, n_other=4):
    rows = []
    for i in range(n_same):
        rows.append({"cik": i + 1, "fiscal_year": 2024, "sic4": "2080", "bucket": "manufacturing",
                     "net_margin": 0.20 + i * 0.001, "roe": 0.30, "leverage": 0.4, "log_assets": 9.5,
                     "log_revenue": 9.2, "gross_margin": 0.45, "operating_margin": 0.25,
                     "asset_turnover": 1.1, "cash_conversion": 1.2, "accruals": -0.02,
                     "fcf_margin": 0.18, "capex_intensity": 0.04, "rd_intensity": 0.01,
                     "sbc_pct_revenue": 0.02, "inventory_intensity": 0.05,
                     "receivables_intensity": 0.04, "dividend_payout": 0.5,
                     "interest_coverage": 20.0, "revenue_yoy": 0.05, "roa": 0.1})
    for i in range(n_other):
        rows.append({"cik": 100 + i, "fiscal_year": 2024, "sic4": "6021", "bucket": "banks",
                     "net_margin": 0.30, "roe": 0.12, "leverage": 0.9, "log_assets": 11.0,
                     "log_revenue": 9.0, "gross_margin": np.nan, "operating_margin": 0.35,
                     "asset_turnover": 0.05, "cash_conversion": 0.9, "accruals": 0.01,
                     "fcf_margin": 0.25, "capex_intensity": 0.01, "rd_intensity": 0.0,
                     "sbc_pct_revenue": 0.01, "inventory_intensity": np.nan,
                     "receivables_intensity": 0.6, "dividend_payout": 0.3,
                     "interest_coverage": 3.0, "revenue_yoy": 0.08, "roa": 0.012})
    return pd.DataFrame(rows)


def test_peers_returns_k_ordered_by_similarity():
    frame = panel()
    result = fingerprint.peers(None, 1, k=3, frame=frame)
    assert len(result) == 3
    sims = list(result["similarity"])
    assert sims == sorted(sims, reverse=True)


def test_target_is_never_its_own_peer():
    result = fingerprint.peers(None, 1, k=8, frame=panel())
    assert 1 not in set(int(x) for x in result["cik"])


def test_same_industry_outranks_other_industry():
    """The first version put brewers next to banks; four-digit SIC fixed that."""
    result = fingerprint.peers(None, 1, k=3, frame=panel())
    assert set(int(x) for x in result["cik"].head(3)) <= {2, 3, 4}


def test_sparse_vectors_are_excluded():
    frame = panel()
    sparse = frame.iloc[[0]].copy()
    sparse["cik"] = 999
    for column in fingerprint.FEATURES:
        if column not in ("log_assets", "log_revenue"):
            sparse[column] = np.nan
    result = fingerprint.peers(None, 1, k=10, frame=pd.concat([frame, sparse], ignore_index=True))
    assert 999 not in set(int(x) for x in result["cik"])


def test_unknown_company_raises_rather_than_returning_nothing():
    import pytest
    with pytest.raises(LookupError):
        fingerprint.peers(None, 424242, k=3, frame=panel())
