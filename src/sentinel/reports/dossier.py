"""The company dossier: the vertical slice that proves the whole pipeline.

Every figure here is a binding. The template cannot print a number unless a query
produced it, which is what makes the verification gate meaningful rather than
theatrical.
"""

from __future__ import annotations

import hashlib
import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import pandas as pd
from jinja2 import Environment, FileSystemLoader, StrictUndefined

from sentinel import PARSER_VERSION
from sentinel.reports import context
from sentinel.reports.render import Binder, write_artifacts
from sentinel.warehouse import db

TEMPLATES = Path(__file__).with_name("templates")

MONEY_ROWS = [
    ("Revenue", "revenue"),
    ("Gross profit", "gross_profit"),
    ("Operating income", "operating_income"),
    ("Net income", "net_income"),
    ("Operating cash flow", "cfo"),
    ("Capital expenditure", "capex"),
]

BALANCE_ROWS = [
    ("Total assets", "assets"),
    ("Stockholders' equity", "equity"),
]

RATIO_ROWS = [
    ("Gross margin", "gross_margin", "pct"),
    ("Operating margin", "operating_margin", "pct"),
    ("Net margin", "net_margin", "pct"),
]


def _env() -> Environment:
    return Environment(
        loader=FileSystemLoader(str(TEMPLATES)),
        undefined=StrictUndefined,
        autoescape=False,
        trim_blocks=True,
        lstrip_blocks=True,
    )


def snapshot_hash(con, cik: int) -> str:
    """A stable fingerprint of the warehouse slice a report was built from."""
    counts = db.table_counts(con)
    payload = json.dumps(sorted(counts.items())).encode("utf-8")
    facts = con.execute(
        "SELECT tag, end_date, value, accession FROM fact_xbrl WHERE cik = ? ORDER BY tag, end_date, accession",
        [cik],
    ).fetchall()
    digest = hashlib.sha256(payload)
    for row in facts:
        digest.update(repr(row).encode("utf-8"))
    return digest.hexdigest()[:16]


def _bind_year_metric(binder: Binder, key: str, metric: str, year: int, fmt: str, refs: list[str]) -> None:
    sql = (
        "SELECT value FROM _canon_rep "
        "WHERE metric_id = ? AND CAST(year(end_date) AS INTEGER) = ?"
    )
    binder.bind(key, sql, [metric, year], fmt=fmt, refs=refs)


def build(con, paths, cik: int, *, as_of: datetime | None = None, years: int = 5) -> dict[str, Any]:
    as_of = as_of or datetime.now(timezone.utc)
    company = db.query(con, "SELECT * FROM dim_company WHERE cik = ?", [cik])
    if company.empty:
        raise LookupError("company " + str(cik) + " is not in the warehouse; run ingest first")
    company_row = company.iloc[0].to_dict()
    ticker = (company_row.get("tickers") or "").split(",")[0]

    frame = context.open_context(con, cik, as_of)
    if frame.empty:
        context.close_context(con)
        raise LookupError("no canonical facts for cik " + str(cik) + " as of " + as_of.isoformat())
    fiscal = context.fiscal_years(frame, limit=years)
    accession_by_key = {
        (row["metric_id"], int(pd.Timestamp(row["end_date"]).year)): row["accession"]
        for _, row in frame.iterrows()
    }

    binder = Binder(con)
    for metric in [m for _, m in MONEY_ROWS + BALANCE_ROWS]:
        for year in fiscal:
            refs = [accession_by_key.get((metric, year)) or ""]
            _bind_year_metric(binder, metric + "_" + str(year), metric, year, "money", [r for r in refs if r])

    for label, metric, fmt in RATIO_ROWS:
        numerator = {"gross_margin": "gross_profit", "operating_margin": "operating_income",
                     "net_margin": "net_income"}[metric]
        for year in fiscal:
            sql = (
                "SELECT n.value / NULLIF(d.value, 0) FROM _canon_rep n, _canon_rep d "
                "WHERE n.metric_id = ? AND CAST(year(n.end_date) AS INTEGER) = ? "
                "AND d.metric_id = 'revenue' AND CAST(year(d.end_date) AS INTEGER) = ?"
            )
            binder.bind(metric + "_" + str(year), sql, [numerator, year, year], fmt=fmt)

    for year in fiscal:
        sql = (
            "SELECT c.value - k.value FROM _canon_rep c, _canon_rep k "
            "WHERE c.metric_id = 'cfo' AND CAST(year(c.end_date) AS INTEGER) = ? "
            "AND k.metric_id = 'capex' AND CAST(year(k.end_date) AS INTEGER) = ?"
        )
        binder.bind("fcf_" + str(year), sql, [year, year], fmt="money")

    if len(fiscal) >= 2:
        first, last = fiscal[-1], fiscal[0]
        sql = (
            "SELECT pow(a.value / NULLIF(b.value, 0), 1.0 / ?) - 1 FROM _canon_rep a, _canon_rep b "
            "WHERE a.metric_id = 'revenue' AND CAST(year(a.end_date) AS INTEGER) = ? "
            "AND b.metric_id = 'revenue' AND CAST(year(b.end_date) AS INTEGER) = ?"
        )
        binder.bind("revenue_cagr", sql, [last - first, last, first], fmt="pct")

    binder.bind("filings_total", "SELECT COUNT(*) FROM fact_filing WHERE cik = ?", [cik], fmt="count")
    binder.bind("accessions_count", "SELECT COUNT(DISTINCT accession) FROM fact_xbrl WHERE cik = ?", [cik], fmt="count")

    forms = db.query(
        con,
        "SELECT form_type, COUNT(*) AS n FROM fact_filing WHERE cik = ? "
        "GROUP BY form_type ORDER BY n DESC LIMIT 6",
        [cik],
    )
    form_rows = []
    for _, row in forms.iterrows():
        key = "form_" + str(row["form_type"]).replace("/", "_")
        binder.bind(key, "SELECT COUNT(*) FROM fact_filing WHERE cik = ? AND form_type = ?",
                    [cik, row["form_type"]], fmt="count")
        form_rows.append({"form": row["form_type"], "key": key})

    sources = db.query(
        con,
        "SELECT DISTINCT source_system, source_url, retrieved_at FROM fact_xbrl WHERE cik = ? "
        "ORDER BY source_system",
        [cik],
    )
    def _stamp(value) -> str | None:
        """One canonical timestamp form in the document: 2026-01-05T00:00:00Z."""
        if not pd.notna(value):
            return None
        ts = pd.Timestamp(value)
        ts = ts.tz_localize("UTC") if ts.tzinfo is None else ts.tz_convert("UTC")
        return ts.strftime("%Y-%m-%dT%H:%M:%SZ")

    provenance_sources = [
        {
            "source": row["source_system"],
            "url": row["source_url"],
            "retrieved_at": _stamp(row["retrieved_at"]),
        }
        for _, row in sources.iterrows()
    ]

    accessions = sorted({a for a in accession_by_key.values() if a})
    snap = snapshot_hash(con, cik)
    generated_at = datetime.now(timezone.utc)

    header_md = "| Metric | " + " | ".join("FY" + str(y) for y in fiscal) + " |"
    sep_md = "|---|" + "|".join(["---:"] * len(fiscal)) + "|"
    rows_money_md = [
        "| " + label + " | " + " | ".join(binder.b(metric + "_" + str(y)) for y in fiscal) + " |"
        for label, metric in MONEY_ROWS + BALANCE_ROWS
    ]
    rows_ratio_md = [
        "| " + label + " | " + " | ".join(binder.b(metric + "_" + str(y)) for y in fiscal) + " |"
        for label, metric, _ in RATIO_ROWS
    ]
    rows_ratio_md.append(
        "| Free cash flow | " + " | ".join(binder.b("fcf_" + str(y)) for y in fiscal) + " |"
    )
    rows_forms_md = ["| " + row["form"] + " | " + binder.b(row["key"]) + " |" for row in form_rows]

    doc = {
        "company": company_row,
        "header_md": header_md,
        "sep_md": sep_md,
        "rows_money_md": rows_money_md,
        "rows_ratio_md": rows_ratio_md,
        "rows_forms_md": rows_forms_md,
        "cik": cik,
        "ticker": ticker,
        "as_of": as_of.date().isoformat(),
        "generated_at": generated_at.isoformat(),
        "snapshot": snap,
        "fiscal_years": fiscal,
        "money_rows": MONEY_ROWS,
        "balance_rows": BALANCE_ROWS,
        "ratio_rows": RATIO_ROWS,
        "form_rows": form_rows,
        "filings_total_key": "filings_total",
        "has_cagr": len(fiscal) >= 2,
        "sources": provenance_sources,
        "accessions": accessions,
        "generator_version": PARSER_VERSION,
    }

    env = _env()
    markdown = binder.finalize(env.get_template("dossier.md.j2").render(b=binder.b, **doc))
    html = binder.finalize(env.get_template("dossier.html.j2").render(b=binder.b, **doc))

    out_dir = Path(paths.reports) / as_of.date().isoformat()
    stem = as_of.date().isoformat() + "_dossier_" + (ticker or str(cik))
    provenance = {
        "kind": "dossier",
        "subject": ticker or str(cik),
        "cik": cik,
        "as_of": as_of.isoformat(),
        "generated_at": generated_at.isoformat(),
        "snapshot_hash": snap,
        "generator_version": PARSER_VERSION,
        "sources": provenance_sources,
        "accessions_used": len(accessions),
        "degraded": [],
    }
    artifacts = write_artifacts(
        out_dir,
        stem=stem,
        markdown=markdown,
        html=html,
        bindings=binder.values(),
        provenance=provenance,
    )
    context.close_context(con)

    db.replace_rows(
        con,
        "report_run",
        pd.DataFrame([{
            "run_id": stem,
            "kind": "dossier",
            "subject": ticker or str(cik),
            "as_of": as_of.date(),
            "generated_at": generated_at.replace(tzinfo=None),
            "snapshot_hash": snap,
            "artifact_paths": json.dumps({k: str(v) for k, v in artifacts.items()}),
            "verified": None,
            "generator_version": PARSER_VERSION,
            "schema_version": 1,
        }]),
        ["run_id"],
    )
    return {"artifacts": artifacts, "provenance": provenance, "bindings": binder.values()}
