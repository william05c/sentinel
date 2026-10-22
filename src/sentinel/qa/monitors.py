"""Data-quality monitors.

Each check writes to dq_issue with a severity so a report can say what was missing
instead of quietly producing a complete-looking document from partial data. A monitor
that finds nothing is a result; a monitor that never ran is not.
"""

from __future__ import annotations

from datetime import datetime, timezone

import pandas as pd

from sentinel.warehouse import db

CHECKS = [
    ("future_dated_facts", "error",
     "SELECT COUNT(*) FROM fact_xbrl WHERE accepted_at > now()"),
    # Facts older than the submissions window have no filing row by design; that is a
    # documented limit, not a defect. An orphan *inside* the window is a real bug.
    ("orphan_facts_in_window", "error",
     "SELECT COUNT(*) FROM fact_xbrl f WHERE NOT EXISTS "
     "(SELECT 1 FROM fact_filing v WHERE v.accession = f.accession) "
     "AND f.filed >= (SELECT MIN(v.filed_date) FROM fact_filing v WHERE v.cik = f.cik)"),
    ("facts_before_filing_window", "info",
     "SELECT COUNT(*) FROM fact_xbrl f WHERE NOT EXISTS "
     "(SELECT 1 FROM fact_filing v WHERE v.accession = f.accession) "
     "AND f.filed < (SELECT MIN(v.filed_date) FROM fact_filing v WHERE v.cik = f.cik)"),
    ("zero_revenue", "warn",
     "SELECT COUNT(*) FROM fact_xbrl_canonical WHERE metric_id = 'revenue' AND value = 0"),
    ("negative_assets", "error",
     "SELECT COUNT(*) FROM fact_xbrl_canonical WHERE metric_id = 'assets' AND value < 0"),
    ("missing_accepted_at", "error",
     "SELECT COUNT(*) FROM fact_xbrl WHERE accepted_at IS NULL"),
    ("duplicate_accession", "error",
     "SELECT COUNT(*) FROM (SELECT accession FROM fact_filing GROUP BY 1 HAVING COUNT(*) > 1)"),
]


def run(con) -> list[dict]:
    """Execute every check and persist the results."""
    db.run(con, "DELETE FROM dq_issue")
    now = datetime.now(timezone.utc).replace(tzinfo=None)
    results: list[dict] = []
    for check_id, severity, sql in CHECKS:
        try:
            count = int(db.scalar(con, sql) or 0)
        except Exception as exc:
            results.append({"check_id": check_id, "severity": "error",
                            "detail": "check failed to run: " + type(exc).__name__})
            continue
        results.append({"check_id": check_id, "severity": severity if count else "info",
                        "detail": f"{count:,} rows" if count else "clean"})
        db.run(
            con,
            "INSERT INTO dq_issue VALUES (?, ?, ?, ?, ?, ?)",
            [check_id, severity if count else "info", None, f"{count} rows", now, 1],
        )

    # coverage by metric, recorded as one row per metric so a report can cite it
    coverage = db.query(con, """
        SELECT metric_id, COUNT(DISTINCT cik) AS companies
        FROM fact_xbrl_canonical GROUP BY metric_id ORDER BY companies DESC
    """)
    total_companies = int(db.scalar(con, "SELECT COUNT(DISTINCT cik) FROM fact_xbrl_canonical") or 1)
    for _, row in coverage.iterrows():
        share = row["companies"] / total_companies
        severity = "warn" if share < 0.25 else "info"
        db.run(
            con,
            "INSERT INTO dq_issue VALUES (?, ?, ?, ?, ?, ?)",
            ["coverage_" + str(row["metric_id"]), severity, None,
             f"{int(row['companies'])}/{total_companies} companies ({share*100:.1f}%)", now, 1],
        )

    # filing gaps: a live company that has not filed in over 400 days is suspicious
    gaps = db.query(con, """
        SELECT c.cik, c.name, MAX(v.filed_date) AS last_filed
        FROM dim_company c JOIN fact_filing v ON v.cik = c.cik
        WHERE c.status = 'active'
        GROUP BY c.cik, c.name
        HAVING date_diff('day', MAX(v.filed_date), current_date) > 400
        ORDER BY last_filed
    """)
    for _, row in gaps.iterrows():
        db.run(
            con,
            "INSERT INTO dq_issue VALUES (?, ?, ?, ?, ?, ?)",
            ["filing_gap", "warn", str(row["cik"]),
             f"last filing {row['last_filed']} for {row['name']}", now, 1],
        )

    return results


def report(con) -> pd.DataFrame:
    return db.query(con, "SELECT check_id, severity, COUNT(*) AS n, MIN(detail) AS example "
                         "FROM dq_issue GROUP BY check_id, severity ORDER BY severity, check_id")
