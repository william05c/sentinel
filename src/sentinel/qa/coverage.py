"""Coverage: what the warehouse actually holds, per company and per metric.

This is the report that stops the project grading itself on effort. It answers three
questions with numbers: how many companies are loaded, how many of them can produce
each core metric, and how much of the universe is still unresolved.
"""

from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path

from sentinel.warehouse import db

CORE_METRICS = ["revenue", "gross_profit", "operating_income", "net_income", "assets", "equity", "cfo", "capex"]


def build(con, paths, *, as_of: datetime | None = None, out_dir: Path | None = None) -> dict:
    as_of = as_of or datetime.now(timezone.utc)
    companies = int(db.scalar(con, "SELECT COUNT(*) FROM dim_company") or 0)
    if companies == 0:
        raise RuntimeError("no companies in the warehouse; run 'sentinel backfill' first")

    status = db.query(con, "SELECT status, COUNT(*) AS n FROM dim_company GROUP BY status ORDER BY 2 DESC")

    rows = []
    for metric in CORE_METRICS:
        with_metric = int(db.scalar(
            con, "SELECT COUNT(DISTINCT cik) FROM fact_xbrl_canonical WHERE metric_id = ?", [metric]
        ) or 0)
        rows.append((metric, with_metric, with_metric / companies))

    spans = db.query(con, """
        SELECT COUNT(*) AS n,
               MIN(fiscal_year) AS first_year,
               MAX(fiscal_year) AS last_year,
               ROUND(AVG(n_years), 1) AS avg_years
        FROM (
            SELECT cik, COUNT(DISTINCT fiscal_year) AS n_years,
                   MIN(fiscal_year) AS fiscal_year, MAX(fiscal_year) AS fiscal_year
            FROM fact_xbrl_canonical WHERE fiscal_year IS NOT NULL GROUP BY cik
        )
    """)

    universe = db.query(con, """
        SELECT COUNT(*) AS intervals,
               SUM(CASE WHEN cik IS NULL THEN 1 ELSE 0 END) AS unresolved,
               SUM(CASE WHEN valid_to IS NULL THEN 1 ELSE 0 END) AS open_intervals
        FROM dim_index_membership WHERE index_code = 'SP500'
    """)

    facts = int(db.scalar(con, "SELECT COUNT(*) FROM fact_xbrl") or 0)
    filings = int(db.scalar(con, "SELECT COUNT(*) FROM fact_filing") or 0)

    stamp = as_of.date().isoformat()
    out_dir = out_dir or (Path(paths.reports) / stamp)
    out_dir.mkdir(parents=True, exist_ok=True)
    path = out_dir / (stamp + "_coverage.md")

    lines = [
        "# Coverage report",
        "",
        "Generated " + as_of.isoformat() + " from the warehouse. Every figure below is a SQL count.",
        "",
        "## Companies",
        "",
        "| Measure | Value |",
        "|---|---:|",
        "| Companies ingested | " + format(companies, ",") + " |",
        "| Filings stored | " + format(filings, ",") + " |",
        "| XBRL facts stored | " + format(facts, ",") + " |",
        "| Universe intervals (S&P 500) | " + format(int(universe.iloc[0]["intervals"]), ",") + " |",
        "| Open intervals | " + format(int(universe.iloc[0]["open_intervals"]), ",") + " |",
        "| Unresolved tickers (no CIK) | " + format(int(universe.iloc[0]["unresolved"] or 0), ",") + " |",
        "",
        "## Status",
        "",
        "| Status | Companies |",
        "|---|---:|",
    ]
    for _, row in status.iterrows():
        lines.append("| " + str(row["status"]) + " | " + format(int(row["n"]), ",") + " |")

    lines += [
        "",
        "## Metric coverage",
        "",
        "Share of ingested companies with at least one canonical value for the metric.",
        "",
        "| Metric | Companies | Coverage |",
        "|---|---:|---:|",
    ]
    for metric, count, share in rows:
        lines.append("| " + metric + " | " + format(count, ",") + " | " + format(share * 100, ".1f") + "% |")

    if not spans.empty:
        row = spans.iloc[0]
        lines += [
            "",
            "## History",
            "",
            "| Measure | Value |",
            "|---|---:|",
            "| Companies with a fiscal year attached | " + format(int(row["n"]), ",") + " |",
            "| Earliest fiscal year | " + str(int(row["first_year"])) + " |",
            "| Latest fiscal year | " + str(int(row["last_year"])) + " |",
            "| Mean years per company | " + str(row["avg_years"]) + " |",
        ]

    lines += ["", "Research output generated from public data. Not investment advice.", ""]
    path.write_text("\n".join(lines), encoding="utf-8")

    return {
        "path": path,
        "companies": companies,
        "filings": filings,
        "facts": facts,
        "coverage": {m: round(s * 100, 1) for m, _, s in rows},
    }
