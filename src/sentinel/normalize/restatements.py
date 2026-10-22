"""Restatement detection.

A company that files the same period twice with different numbers is doing something
worth knowing about. The warehouse keeps every version, so a restatement is a query
rather than an overwrite - which is the difference between a history and a snapshot.
"""

from __future__ import annotations

from sentinel.warehouse import db

SQL = """
WITH ordered AS (
    SELECT cik, taxonomy, tag, unit, period_type, start_date, end_date, value, accession,
           accepted_at, form,
           LAG(value) OVER w AS prior_value,
           LAG(accession) OVER w AS prior_accession,
           LAG(accepted_at) OVER w AS prior_accepted
    FROM fact_xbrl
    WHERE end_date IS NOT NULL AND accepted_at IS NOT NULL
    -- unit belongs in the partition: the same tag can be reported in USD and in shares,
    -- and comparing those produces a "restatement" that is really a unit change.
    WINDOW w AS (PARTITION BY cik, taxonomy, tag, unit, period_type, start_date, end_date
                 ORDER BY accepted_at, accession)
)
SELECT cik, tag, unit, period_type, end_date, form, accession, accepted_at,
       prior_accession, prior_value, value,
       value - prior_value AS delta,
       CASE WHEN prior_value <> 0 THEN (value - prior_value) / abs(prior_value) END AS pct_change,
       -- A 10x/100x/1000x jump with the same tag and unit is almost always a scale
       -- error or a split, not an accounting restatement. Both are worth seeing; they
       -- are not the same finding.
       CASE WHEN greatest(abs(value), abs(prior_value))
                 / nullif(least(abs(value), abs(prior_value)), 0) >= 10
            THEN 'scale_change' ELSE 'value_change' END AS kind
FROM ordered
WHERE prior_value IS NOT NULL AND value <> prior_value
ORDER BY abs(value - prior_value) DESC
"""


def detect(con, *, limit: int | None = None, min_pct: float | None = None):
    """Every changed value between two filings of the same period."""
    frame = db.query(con, SQL)
    if frame.empty:
        return frame
    if min_pct is not None:
        frame = frame[frame["pct_change"].abs() >= min_pct]
    if limit is not None:
        frame = frame.head(limit)
    return frame


def summary(con) -> dict:
    total = int(db.scalar(con, "SELECT COUNT(*) FROM fact_xbrl") or 0)
    frame = detect(con)
    if frame.empty:
        return {"facts": total, "restated_values": 0, "scale_changes": 0,
                "companies_affected": 0, "share_of_facts": 0.0}
    real = frame[frame["kind"] == "value_change"]
    return {
        "facts": total,
        "value_changes": int(len(real)),
        "scale_changes": int((frame["kind"] == "scale_change").sum()),
        "companies_affected": int(real["cik"].nunique()),
        "share_of_facts": round(len(real) / total * 100, 3) if total else 0.0,
    }
