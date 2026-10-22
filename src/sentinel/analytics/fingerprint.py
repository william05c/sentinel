"""Company fingerprints and peer matching.

A fingerprint is a vector of industry-neutralised ratios for one company-year. Two
companies are "similar" if those vectors point the same way. Nothing here is a
valuation opinion; it is a statement about operating shape.

The design constraint that matters: ratios are z-scored INSIDE an SIC bucket, so a
bank is never compared with a manufacturer on leverage. When a company has too few
peers in its bucket the z-score is NaN, and the fingerprint falls back to the raw
ratio rather than pretending the normalisation happened.
"""

from __future__ import annotations

import numpy as np
import pandas as pd

from sentinel.features import derived

FEATURES = [
    "gross_margin", "operating_margin", "net_margin", "roa", "roe",
    "leverage", "asset_turnover", "cash_conversion", "accruals",
    "fcf_margin", "sbc_pct_revenue", "dividend_payout", "interest_coverage",
    "revenue_yoy",
]


def panel(con, *, year: int | None = None) -> pd.DataFrame:
    """Derived features plus industry z-scores, optionally for one fiscal year."""
    from sentinel.normalize import canonical

    canonical_rows = con.execute("SELECT * FROM fact_xbrl_canonical").fetch_df()
    if canonical_rows.empty:
        return canonical_rows
    wide = derived.pivot_annual(canonical_rows)
    out = derived.growth(derived.compute(wide))
    # A fiscal year counts only if an annual income statement exists for it. Without
    # this filter every company's "latest year" is the current partial year, whose
    # vector is mostly missing and therefore mostly imputed - which is how the peer
    # list turned into noise on the first attempt.
    out = out[out["revenue"].notna() | out["net_income"].notna()]
    sectors = con.execute("SELECT cik, sic FROM dim_company").fetch_df()
    out = derived.industry_zscore(out, sectors, FEATURES)
    if year is not None:
        out = out[out["fiscal_year"] == year]
    return out


def vector(row: pd.Series, features: list[str] | None = None) -> np.ndarray:
    """Feature vector, preferring the industry-neutralised z-score when present."""
    features = features or FEATURES
    values = []
    for name in features:
        z = row.get(name + "_z")
        v = z if z is not None and pd.notna(z) else row.get(name)
        values.append(float(v) if v is not None and pd.notna(v) else 0.0)
    return np.asarray(values, dtype=float)


def peers(con, cik: int, k: int = 10, year: int | None = None,
          frame: pd.DataFrame | None = None) -> pd.DataFrame:
    """Nearest companies by cosine similarity of their fingerprints.

    The panel is expensive to build (one pivot over the whole canonical table) and is
    therefore reusable: precision_at_k passes one frame for all pairs instead of
    rebuilding it per company.
    """
    frame = frame if frame is not None else panel(con, year=year)
    if frame.empty:
        return frame
    target = frame[frame["cik"] == cik]
    if target.empty:
        raise LookupError(f"no fingerprint for cik {cik}")
    target_year = int(target["fiscal_year"].max())

    # Each company contributes its most recent year within a one-year window of the
    # target. Requiring an exact fiscal-year match silently excluded every peer whose
    # year-end differed by a month, which is most of them.
    latest = frame.sort_values("fiscal_year").groupby("cik", as_index=False).tail(1)
    latest = latest[latest["fiscal_year"].between(target_year - 1, target_year)]

    rows = latest[["cik", "fiscal_year", "bucket", "net_margin", "roe", "leverage"]].reset_index(drop=True)
    values = []
    for _, row in latest.iterrows():
        values.append([row.get(f + "_z") if pd.notna(row.get(f + "_z")) else row.get(f) for f in FEATURES])
    matrix = pd.DataFrame(values, columns=FEATURES).astype(float)

    # Median imputation inside the cross-section, plus a completeness filter. Filling
    # missing features with zero made every sparse company look identical, which is
    # exactly how a peer list degenerates into noise.
    matrix = matrix.fillna(matrix.median(numeric_only=True)).fillna(0.0)
    keep = matrix.notna().all(axis=1)
    matrix = matrix[keep]
    rows = rows[keep].reset_index(drop=True)
    if matrix.empty:
        return rows

    index = rows.index[rows["cik"] == cik]
    if len(index) == 0:
        raise LookupError(f"cik {cik} has no complete fingerprint in {target_year}")
    i = int(index[0])

    norms = np.linalg.norm(matrix.to_numpy(), axis=1)
    a = matrix.to_numpy()[i]
    denom = norms * np.linalg.norm(a)
    similarity = np.divide(matrix.to_numpy() @ a, denom, out=np.zeros(len(matrix)), where=denom > 0)
    scored = rows.assign(similarity=similarity)
    scored = scored[scored["cik"] != cik]
    return scored.sort_values("similarity", ascending=False).head(k)[
        ["cik", "fiscal_year", "similarity", "bucket", "net_margin", "roe", "leverage"]
    ]


def precision_at_k(con, labelled: dict[int, int], k: int = 10, year: int | None = None) -> dict:
    """Fraction of companies whose hand-labelled peer appears in their top k.

    The labels are judgement calls about who competes with whom, made before looking at
    the output. That ordering is the whole point: a peer list that is validated after
    seeing it proves nothing.
    """
    hits = 0
    checked = 0
    details = []
    shared = panel(con, year=year)
    for cik, expected in labelled.items():
        try:
            table = peers(con, cik, k=k, year=year, frame=shared)
        except LookupError:
            continue
        checked += 1
        found = expected in set(int(x) for x in table["cik"])
        hits += int(found)
        details.append({"cik": cik, "expected": expected, "hit": found,
                        "top": [int(x) for x in table["cik"].head(3)]})
    return {
        "k": k,
        "pairs": checked,
        "hits": hits,
        "precision_at_k": round(hits / checked, 3) if checked else 0.0,
        "details": details,
    }
