"""Canonical metric selection.

This is the part of the pipeline where correctness is won or lost. Three rules:

1. A metric is built from an ORDERED chain of candidate XBRL tags. Revenue is not
   one tag; it is a preference order that changed when ASC 606 landed.
2. Every version of a fact is kept. Selection happens at query time against an
   as-of timestamp, so a restatement cannot retroactively contaminate a 2020 view.
3. The chosen tag is recorded. A company that silently switches tags mid-history is
   itself a finding, not a formatting detail.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

import pandas as pd

from sentinel import SCHEMA_VERSION
from sentinel.warehouse import db

BUCKET_SQL = """
CASE
    WHEN period_type = 'instant' THEN 'I'
    WHEN dur_days BETWEEN 350 AND 380 THEN 'FY'
    WHEN dur_days BETWEEN 80 AND 100 THEN 'Q'
    ELSE 'X'
END
"""

CANONICAL_SQL = """
WITH cand AS (
    SELECT x.cik, x.metric_id, x.period_type, x.start_date, x.end_date,
           x.fiscal_year, x.fiscal_period, x.value, x.unit, x.tag, x.priority,
           x.accession, x.accepted_at,
           CAST(date_diff('day', x.start_date, x.end_date) AS INTEGER) AS dur_days
    FROM (
        SELECT f.*, m.metric_id, m.priority
        FROM fact_xbrl f
        JOIN map_tag m ON m.taxonomy = f.taxonomy AND m.tag = f.tag
        WHERE f.cik = ? AND f.accepted_at <= ?
    ) x
),
bucketed AS (
    SELECT *, {bucket} AS bucket
    FROM cand
    WHERE period_type = 'instant'
       OR (dur_days IS NOT NULL AND dur_days BETWEEN 80 AND 380)
),
ranked AS (
    SELECT *,
           ROW_NUMBER() OVER (
               PARTITION BY metric_id, period_type, end_date, bucket
               ORDER BY priority ASC, accepted_at DESC, accession DESC
           ) AS rn,
           COUNT(*) OVER (
               PARTITION BY metric_id, period_type, end_date, bucket
           ) AS n_candidates
    FROM bucketed
)
SELECT cik, metric_id, period_type, start_date, end_date, fiscal_year, fiscal_period,
       value, unit, tag AS tag_used, priority, n_candidates, accession, accepted_at
FROM ranked
WHERE rn = 1
ORDER BY metric_id, period_type, end_date
"""


def tag_map_frame(metrics_doc: dict) -> pd.DataFrame:
    """Flatten config/metrics.yaml into the map_tag table.

    Priority is the position in the chain: 0 is most preferred. The same tag may
    appear in two metrics; the last declaration wins, and that is a config error we
    surface rather than hide.
    """
    rows: list[dict[str, Any]] = []
    seen: dict[tuple[str, str], str] = {}
    for metric in metrics_doc.get("metrics", []):
        for priority, tag in enumerate(metric.get("tags", [])):
            key = ("us-gaap", tag)
            if key in seen and seen[key] != metric["id"]:
                raise ValueError(f"tag {tag} declared for both {seen[key]} and {metric['id']}")
            seen[key] = metric["id"]
            rows.append({
                "taxonomy": "us-gaap",
                "tag": tag,
                "metric_id": metric["id"],
                "priority": priority,
                "sign": 1,
                "unit": metric.get("unit"),
                "notes": metric.get("definition"),
            })
    return pd.DataFrame(rows)


def build_canonical(
    con,
    cik: int,
    as_of: datetime | None = None,
) -> pd.DataFrame:
    """Select one value per (metric, period) using only facts known as of as_of."""
    as_of = as_of or datetime.now(timezone.utc)
    if as_of.tzinfo is not None:
        as_of = as_of.astimezone(timezone.utc).replace(tzinfo=None)
    sql = CANONICAL_SQL.format(bucket=BUCKET_SQL)
    frame = db.query(con, sql, [cik, as_of])
    if frame.empty:
        return frame
    frame["built_at"] = datetime.now(timezone.utc)
    frame["schema_version"] = SCHEMA_VERSION
    return frame


def materialize(con, cik: int, as_of: datetime | None = None) -> int:
    """Write the canonical view for one company into fact_xbrl_canonical.

    The table is a cache of one particular as-of. Reports that need a different
    as-of rebuild from fact_xbrl rather than trusting this table, which is why the
    as-of test cannot be satisfied by a stale materialisation.
    """
    as_of = as_of or datetime.now(timezone.utc)
    frame = build_canonical(con, cik, as_of)
    if frame.empty:
        return 0
    db.run(con, "DELETE FROM fact_xbrl_canonical WHERE cik = ?", [cik])
    con.register("_canon", frame)
    try:
        cols = ", ".join(frame.columns)
        db.run(con, "INSERT INTO fact_xbrl_canonical (" + cols + ") SELECT " + cols + " FROM _canon")
    finally:
        con.unregister("_canon")
    return len(frame)


def annual_panel(frame: pd.DataFrame) -> pd.DataFrame:
    """Restrict a canonical frame to annual durations and period-end instants.

    Fiscal years are identified by the FY bucket rather than by calendar arithmetic,
    because a 52/53-week retailer's fiscal 2024 does not end in December.
    """
    if frame.empty:
        return frame
    if "dur_days" not in frame.columns:
        frame = frame.copy()
        frame["dur_days"] = (
            pd.to_datetime(frame["end_date"]) - pd.to_datetime(frame["start_date"])
        ).dt.days
    is_fy = (frame["period_type"] == "duration") & frame["dur_days"].between(350, 380)
    is_instant = frame["period_type"] == "instant"
    return frame[is_fy | is_instant].copy()
