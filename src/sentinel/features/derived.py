"""Derived features and industry-relative scoring.

Two rules govern everything here:

1. A ratio without its inputs recorded is a rumour. Every feature carries the metrics
   it consumed and returns NULL when an input is missing - never zero, never a silent
   substitution.
2. A ratio compared across the whole market is close to meaningless. Margins, turns and
   leverage differ structurally by industry, so the z-scores are computed inside SIC
   buckets and say so.

The functions are pure: canonical frames in, frames out. No I/O, no database.
"""

from __future__ import annotations

import numpy as np
import pandas as pd

# SIC divisions, used as the industry bucket. Coarse on purpose: a finer bucket would
# leave too few peers per industry for a stable z-score at this universe size.
SIC_BUCKETS = [
    (100, 999, "agriculture"),
    (1000, 1499, "mining"),
    (1500, 1799, "construction"),
    (2000, 3999, "manufacturing"),
    (4000, 4999, "transport_utilities"),
    (5000, 5199, "wholesale"),
    (5200, 5999, "retail"),
    (6000, 6199, "banks"),
    (6200, 6299, "securities"),
    (6300, 6499, "insurance"),
    (6500, 6599, "real_estate"),
    (6700, 6799, "holding_reits"),
    (7000, 8999, "services"),
    (9100, 9999, "public_admin"),
]


def sic_bucket(sic: str | int | None) -> str:
    if sic is None or (isinstance(sic, float) and np.isnan(sic)):
        return "unknown"
    try:
        code = int(str(sic).strip()[:4])
    except (TypeError, ValueError):
        return "unknown"
    for lo, hi, name in SIC_BUCKETS:
        if lo <= code <= hi:
            return name
    return "unknown"


def pivot_annual(canonical: pd.DataFrame) -> pd.DataFrame:
    """One row per (cik, period year) with a column per metric.

    The year comes from the PERIOD END DATE, not from the XBRL fiscal-year tag. That tag
    belongs to the filing, not the fact: Apple's FY2025 10-K reports FY2023 revenue of
    $383.3B under fy=2025, so grouping by it would file a 2023 number under 2025 and
    silently misalign every derived ratio.

    Only annual durations and instants are kept, so a Q1 figure cannot stand in for a
    year via an aggregation order.
    """
    if canonical.empty:
        return canonical
    frame = canonical[canonical["end_date"].notna()].copy()
    start = pd.to_datetime(frame["start_date"])
    end = pd.to_datetime(frame["end_date"])
    frame["dur_days"] = (end - start).dt.days
    is_annual = (frame["period_type"] == "duration") & frame["dur_days"].between(350, 380)
    is_instant = frame["period_type"] == "instant"
    frame = frame[is_annual | is_instant]
    frame["fiscal_year"] = end.dt.year
    frame = frame[frame["fiscal_year"].between(1995, 2100)]
    wide = frame.pivot_table(
        index=["cik", "fiscal_year"], columns="metric_id", values="value", aggfunc="last"
    ).reset_index()
    wide.columns.name = None
    return wide


def _safe_div(numerator, denominator):
    num = pd.to_numeric(numerator, errors="coerce")
    den = pd.to_numeric(denominator, errors="coerce")
    return np.where((den.notna()) & (den != 0), num / den, np.nan)


def compute(wide: pd.DataFrame, fiscal_year_end: pd.Series | None = None) -> pd.DataFrame:
    """Add derived columns. Missing inputs propagate to NaN, which is the honest answer."""
    out = wide.copy()
    for col in ("revenue", "gross_profit", "operating_income", "net_income", "assets",
                "equity", "liabilities", "cfo", "capex", "inventory", "receivables",
                "accounts_payable", "long_term_debt", "total_debt", "cash", "interest_expense",
                "tax_expense", "pretax_income", "sbc", "buybacks", "dividends_paid"):
        if col not in out.columns:
            out[col] = np.nan

    out["gross_margin"] = _safe_div(out["gross_profit"], out["revenue"])
    out["operating_margin"] = _safe_div(out["operating_income"], out["revenue"])
    out["net_margin"] = _safe_div(out["net_income"], out["revenue"])
    out["fcf"] = out["cfo"] - out["capex"]
    out["fcf_margin"] = _safe_div(out["fcf"], out["revenue"])
    out["asset_turnover"] = _safe_div(out["revenue"], out["assets"])
    out["leverage"] = _safe_div(out["liabilities"], out["assets"])
    out["debt_to_equity"] = _safe_div(out["total_debt"], out["equity"])
    out["roa"] = _safe_div(out["net_income"], out["assets"])
    out["roe"] = _safe_div(out["net_income"], out["equity"])
    out["invested_capital"] = out["total_debt"].fillna(0) + out["equity"]
    out["nopat"] = out["operating_income"] * (1.0 - _safe_div(out["tax_expense"], out["pretax_income"]))
    out["roic"] = _safe_div(out["nopat"], out["invested_capital"])
    out["cash_conversion"] = _safe_div(out["cfo"], out["net_income"])
    out["accruals"] = _safe_div(out["net_income"] - out["cfo"], out["assets"])
    out["dso"] = _safe_div(out["receivables"], out["revenue"]) * 365.0
    out["dio"] = _safe_div(out["inventory"], out["gross_profit"].replace(0, np.nan)) * 365.0
    out["dpo"] = _safe_div(out["accounts_payable"], out["gross_profit"].replace(0, np.nan)) * 365.0
    out["cash_cycle"] = out["dso"] + out["dio"] - out["dpo"]
    out["sbc_pct_revenue"] = _safe_div(out["sbc"], out["revenue"])
    out["buyback_pct_revenue"] = _safe_div(out["buybacks"], out["revenue"])
    out["dividend_payout"] = _safe_div(out["dividends_paid"], out["net_income"])
    out["interest_coverage"] = _safe_div(out["operating_income"], out["interest_expense"].abs())
    return out


def growth(out: pd.DataFrame) -> pd.DataFrame:
    """Year-on-year growth and a compound rate over the available history."""
    frame = out.sort_values(["cik", "fiscal_year"]).copy()
    for col in ("revenue", "net_income", "cfo", "assets"):
        frame[col + "_yoy"] = frame.groupby("cik")[col].pct_change()
    frame["revenue_cagr_3y"] = np.nan
    for cik, block in frame.groupby("cik"):
        block = block.sort_values("fiscal_year")
        years = block["fiscal_year"].to_numpy()
        revenue = block["revenue"].to_numpy(dtype=float)
        for i in range(len(years)):
            prior_idx = np.where(years == years[i] - 3)[0]
            if len(prior_idx) == 0:
                continue
            j = prior_idx[0]
            if revenue[j] and revenue[j] > 0 and revenue[i] and revenue[i] > 0:
                frame.loc[block.index[i], "revenue_cagr_3y"] = (revenue[i] / revenue[j]) ** (1 / 3) - 1
    return frame


def industry_zscore(out: pd.DataFrame, sectors: pd.DataFrame, columns: list[str]) -> pd.DataFrame:
    """Z-score each feature inside its SIC bucket, per fiscal year.

    A z-score needs peers. A bucket-year with fewer than 5 companies returns NaN rather
    than a number computed from three names and a wish.
    """
    frame = out.merge(sectors, on="cik", how="left")
    frame["bucket"] = frame["sic"].map(sic_bucket)
    MIN_PEERS = 5
    for col in columns:
        grouped = frame.groupby(["bucket", "fiscal_year"])[col]
        mean = grouped.transform("mean")
        std = grouped.transform("std")
        count = grouped.transform("count")
        z = (frame[col] - mean) / std
        frame[col + "_z"] = np.where((count >= MIN_PEERS) & (std > 0), z, np.nan)
    return frame
