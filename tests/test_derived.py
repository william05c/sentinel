"""Derived features: the arithmetic, and the honesty of its failure modes."""

from __future__ import annotations

import numpy as np
import pandas as pd

from sentinel.features import derived


def test_sic_bucket_maps_divisions():
    assert derived.sic_bucket("3571") == "manufacturing"
    assert derived.sic_bucket(6022) == "banks"
    assert derived.sic_bucket("6311") == "insurance"
    assert derived.sic_bucket("6798") == "holding_reits"
    assert derived.sic_bucket(None) == "unknown"
    assert derived.sic_bucket("") == "unknown"
    assert derived.sic_bucket("abc") == "unknown"
    # a five-digit code is truncated to its division prefix, as SEC filings sometimes pad
    assert derived.sic_bucket("99999") == "public_admin"


def test_ratios_compute_from_inputs():
    wide = pd.DataFrame([{
        "cik": 1, "fiscal_year": 2024, "revenue": 1000.0, "gross_profit": 400.0,
        "operating_income": 200.0, "net_income": 120.0, "assets": 800.0,
        "equity": 300.0, "liabilities": 500.0, "cfo": 150.0, "capex": 50.0,
    }])
    out = derived.compute(wide).iloc[0]
    assert out["gross_margin"] == 0.4
    assert out["net_margin"] == 0.12
    assert out["fcf"] == 100.0
    assert out["roa"] == 0.15
    assert abs(out["leverage"] - 0.625) < 1e-9
    assert abs(out["accruals"] - (120 - 150) / 800) < 1e-9


def test_missing_input_gives_nan_not_zero():
    """A missing metric must never become zero, because zero is a claim."""
    wide = pd.DataFrame([{"cik": 1, "fiscal_year": 2024, "revenue": 1000.0}])
    out = derived.compute(wide).iloc[0]
    assert np.isnan(out["gross_margin"])
    assert np.isnan(out["roa"])
    assert out["fcf"] != 0  # cfo and capex both absent -> NaN - NaN
    assert np.isnan(out["fcf"])


def test_zero_denominator_is_nan():
    wide = pd.DataFrame([{"cik": 1, "fiscal_year": 2024, "revenue": 0.0, "net_income": 5.0}])
    out = derived.compute(wide).iloc[0]
    assert np.isnan(out["net_margin"])


def test_industry_zscore_needs_peers():
    rows = []
    for i in range(8):
        rows.append({"cik": i, "fiscal_year": 2024, "net_margin": 0.1 + i * 0.01})
    sectors = pd.DataFrame({"cik": list(range(8)), "sic": ["3571"] * 8})
    out = derived.industry_zscore(pd.DataFrame(rows), sectors, ["net_margin"])
    assert out["net_margin_z"].notna().all()

    # only two peers in the bucket: no z-score is defensible
    thin = derived.industry_zscore(pd.DataFrame(rows[:2]), sectors.head(2), ["net_margin"])
    assert thin["net_margin_z"].isna().all()


def test_growth_uses_actual_fiscal_years():
    wide = pd.DataFrame([
        {"cik": 1, "fiscal_year": 2021, "revenue": 100.0},
        {"cik": 1, "fiscal_year": 2022, "revenue": 110.0},
        {"cik": 1, "fiscal_year": 2023, "revenue": 121.0},
        {"cik": 1, "fiscal_year": 2024, "revenue": 133.1},
    ])
    out = derived.growth(derived.compute(wide))
    assert abs(out.loc[out["fiscal_year"] == 2024, "revenue_cagr_3y"].iloc[0] - 0.1) < 1e-9
