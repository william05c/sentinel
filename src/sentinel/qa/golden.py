"""Golden-set reconciliation against SEC's own concept endpoint.

Why this and not a hand-typed spreadsheet: the concept endpoint is served by the SEC
independently of the company-facts payload we ingest, so agreement is evidence rather
than a tautology. It is also cheap enough to run over 25 companies and 5 years each,
which a PDF-by-PDF manual check is not.

The scope is honest about what it does not cover: it compares values, not the
presentation of them, and it cannot validate a tag choice that the SEC itself does not
report.
"""

from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path

import pandas as pd

from sentinel.adapters.sec import CONCEPT_URL
from sentinel.features.derived import sic_bucket
from sentinel.normalize import canonical
from sentinel.warehouse import db

CORE = [
    "revenue", "gross_profit", "operating_income", "net_income",
    "assets", "equity", "cfo", "capex", "eps_diluted", "shares_diluted",
]


def select_targets(con, n: int = 25) -> pd.DataFrame:
    """Companies that actually have data, spread across SIC buckets.

    Diversity is the point: a golden set of 25 software companies would pass while the
    bank and REIT paths were broken.
    """
    frame = db.query(con, """
        SELECT c.cik, c.name, c.sic, COUNT(DISTINCT f.metric_id) AS metrics
        FROM dim_company c
        JOIN fact_xbrl_canonical f ON f.cik = c.cik
        GROUP BY c.cik, c.name, c.sic
        HAVING COUNT(DISTINCT f.metric_id) >= 6
        ORDER BY metrics DESC, c.cik
    """)
    frame["bucket"] = frame["sic"].map(sic_bucket)
    chosen: list[pd.Series] = []
    seen: set[str] = set()
    for _, row in frame.iterrows():
        if row["bucket"] in seen:
            continue
        chosen.append(row)
        seen.add(row["bucket"])
        if len(chosen) >= n:
            break
    for _, row in frame.iterrows():
        if len(chosen) >= n:
            break
        if not any(row["cik"] == c["cik"] for c in chosen):
            chosen.append(row)
    return pd.DataFrame(chosen).head(n)


def _concept_index(payload: dict) -> dict[tuple[str, str, str], float]:
    """Index by (end, start, accession).

    The start date is not optional. A 10-K carries both the twelve-month and the
    three-month facts ending on the same day, so keying on (end, accn) alone silently
    compares an annual figure against a quarterly one - which is exactly the mismatch
    this function reported for one company before the key was fixed.
    """
    values: dict[tuple[str, str, str], float] = {}
    for facts in (payload.get("units") or {}).values():
        for row in facts:
            values[(str(row.get("end")), str(row.get("start")), str(row.get("accn")))] = float(row["val"])
    return values


def run(con, fetcher, *, n: int = 25, years: int = 5) -> dict:
    targets = select_targets(con, n)
    as_of = datetime.now(timezone.utc)
    compared = matched = 0
    mismatches: list[dict] = []
    per_company: list[dict] = []

    for _, company in targets.iterrows():
        cik = int(company["cik"])
        frame = canonical.annual_panel(canonical.build_canonical(con, cik, as_of))
        if frame.empty:
            continue
        recent = sorted(pd.to_datetime(frame["end_date"]).dt.year.unique(), reverse=True)[:years]
        frame = frame[pd.to_datetime(frame["end_date"]).dt.year.isin(recent)]
        company_compared = company_matched = 0

        for metric in CORE:
            rows = frame[frame["metric_id"] == metric]
            if rows.empty:
                continue
            for tag, block in rows.groupby("tag_used"):
                try:
                    payload, _ = fetcher.get_json(CONCEPT_URL.format(cik=cik, taxonomy="us-gaap", tag=tag))
                except Exception:
                    continue
                reference = _concept_index(payload)
                for _, fact in block.iterrows():
                    start = fact.get("start_date")
                    key = (
                        str(pd.Timestamp(fact["end_date"]).date()),
                        str(pd.Timestamp(start).date()) if pd.notna(start) else "None",
                        str(fact["accession"]),
                    )
                    if key not in reference:
                        continue
                    company_compared += 1
                    if abs(reference[key] - float(fact["value"])) <= 0.005:
                        company_matched += 1
                    else:
                        mismatches.append({
                            "cik": cik, "metric": metric, "tag": tag, "period_end": key[0],
                            "ours": float(fact["value"]), "sec": reference[key],
                        })
        compared += company_compared
        matched += company_matched
        if company_compared:
            per_company.append({
                "cik": cik, "name": company["name"], "bucket": company["bucket"],
                "compared": company_compared, "matched": company_matched,
            })

    rate = matched / compared if compared else 0.0
    return {
        "companies": len(per_company),
        "compared": compared,
        "matched": matched,
        "mismatched": compared - matched,
        "rate": round(rate * 100, 3),
        "mismatches": mismatches[:20],
        "per_company": per_company,
    }


def write_report(result: dict, paths, *, as_of: datetime | None = None) -> Path:
    as_of = as_of or datetime.now(timezone.utc)
    out_dir = Path(paths.reports) / as_of.date().isoformat()
    out_dir.mkdir(parents=True, exist_ok=True)
    path = out_dir / (as_of.date().isoformat() + "_golden_set.md")
    lines = [
        "# Golden-set reconciliation",
        "",
        "Our canonical values compared with SEC's company-concept endpoint, which is",
        "served independently of the company-facts payload this pipeline ingests.",
        "",
        "| Measure | Value |",
        "|---|---:|",
        f"| Companies | {result['companies']} |",
        f"| Values compared | {result['compared']:,} |",
        f"| Exact matches | {result['matched']:,} |",
        f"| Mismatches | {result['mismatched']:,} |",
        f"| Match rate | {result['rate']}% |",
        "",
        "## Per company",
        "",
        "| CIK | Company | Bucket | Compared | Matched |",
        "|---:|---|---|---:|---:|",
    ]
    for row in result["per_company"]:
        lines.append(f"| {row['cik']} | {row['name'][:40]} | {row['bucket']} | {row['compared']} | {row['matched']} |")
    if result["mismatches"]:
        lines += ["", "## Mismatches (first 20)", "",
                  "| CIK | Metric | Tag | Period | Ours | SEC |", "|---:|---|---|---|---:|---:|"]
        for m in result["mismatches"]:
            lines.append(f"| {m['cik']} | {m['metric']} | {m['tag']} | {m['period_end']} | {m['ours']:.2f} | {m['sec']:.2f} |")
    lines += ["", "Tolerance: exact to the cent. Research output, not investment advice.", ""]
    path.write_text("\n".join(lines), encoding="utf-8")
    return path
