"""Targets: the actual files behind a company, so a crawl can be aimed at them.

"Identify the files I want to target" is a concrete request, and it deserves a concrete
answer: a manifest of real documents with real URLs, sizes and accessions, not a list of
abstract sources.

Two kinds of target:
    filing   one primary document from one accession, e.g. Apple's FY2025 10-K
    bulk     a quarterly SEC archive that contains thousands of filing facts at once
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path

import pandas as pd

from sentinel.warehouse import db

FILING_URL = "https://www.sec.gov/Archives/edgar/data/{cik}/{accession}/{document}"


def filing_targets(con, cik: int, *, forms: list[str] | None = None, limit: int = 50) -> list[dict]:
    """Every stored filing that has a primary document, newest first."""
    params: list = [cik]
    clause = ""
    if forms:
        clause = " AND form_type IN (" + ",".join("?" * len(forms)) + ")"
        params.extend(forms)
    params.append(limit)
    rows = db.query(con, """
        SELECT accession, form_type, filed_date, period_end, primary_document, primary_desc, items
        FROM fact_filing
        WHERE cik = ? AND primary_document IS NOT NULL
    """ + clause + " ORDER BY filed_date DESC LIMIT ?", params)
    out = []
    for _, row in rows.iterrows():
        out.append({
            "kind": "filing",
            "form": row["form_type"],
            "filed": str(row["filed_date"])[:10],
            "period_end": str(row["period_end"] or "")[:10],
            "accession": row["accession"],
            "document": row["primary_document"],
            "url": FILING_URL.format(cik=cik, accession=str(row["accession"]).replace("-", ""),
                                     document=row["primary_document"]),
            "description": (row["primary_desc"] or "")[:80],
            "items": row["items"] or "",
        })
    return out


def bulk_targets(years_back: int = 2) -> list[dict]:
    """Quarterly SEC archives. One download covers every filer for that quarter."""
    today = datetime.now(timezone.utc).date()
    out = []
    for i in range(years_back * 4):
        month = today.month - i * 3
        year = today.year + (month - 1) // 12
        month = (month - 1) % 12 + 1
        quarter = (month - 1) // 3 + 1
        label = str(year) + "q" + str(quarter)
        out.append({
            "kind": "bulk",
            "form": "financial-statement-data-set",
            "filed": label,
            "period_end": label,
            "accession": "",
            "document": label + ".zip",
            "url": "https://www.sec.gov/files/dera/data/financial-statement-data-sets/" + label + ".zip",
            "description": "quarterly numeric facts for every filer",
            "items": "",
        })
    return out


def build_manifest(con, paths, ticker: str | None = None, cik: int | None = None,
                   forms: list[str] | None = None, limit: int = 50, include_bulk: bool = True) -> dict:
    if cik is None:
        row = db.query(con, "SELECT cik, name, tickers FROM dim_company WHERE upper(tickers) = ? LIMIT 1",
                       [(ticker or "").upper()])
        if row.empty:
            raise LookupError("unknown ticker " + str(ticker))
        cik = int(row.iloc[0]["cik"])
        name = row.iloc[0]["name"]
    else:
        name = str(db.scalar(con, "SELECT name FROM dim_company WHERE cik = ?", [cik]) or cik)

    targets = filing_targets(con, cik, forms=forms, limit=limit)
    if include_bulk:
        targets += bulk_targets()
    manifest = {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "company": {"name": name, "cik": cik, "ticker": ticker or ""},
        "counts": {"filing": sum(1 for t in targets if t["kind"] == "filing"),
                   "bulk": sum(1 for t in targets if t["kind"] == "bulk")},
        "targets": targets,
    }
    out_dir = Path(paths.root) / "viz"
    out_dir.mkdir(parents=True, exist_ok=True)
    (out_dir / "targets.json").write_text(json.dumps(manifest, indent=1), encoding="utf-8")
    report_dir = Path(paths.reports) / datetime.now(timezone.utc).date().isoformat()
    report_dir.mkdir(parents=True, exist_ok=True)
    pd.DataFrame(targets).to_csv(report_dir / "targets.csv", index=False)
    return manifest


def download(con, fetcher, paths, manifest: dict, *, limit: int = 5) -> list[dict]:
    """Fetch selected targets into data/files/. Polite, because it goes through the fetcher."""
    out_dir = Path(paths.data) / "files"
    out_dir.mkdir(parents=True, exist_ok=True)
    saved = []
    for target in [t for t in manifest["targets"] if t["kind"] == "filing"][:limit]:
        try:
            result = fetcher.fetch(target["url"])
        except Exception as exc:
            saved.append({"url": target["url"], "error": type(exc).__name__ + ": " + str(exc)[:80]})
            continue
        name = (target["form"] + "_" + target["filed"] + "_" + target["accession"] + "_" +
                target["document"]).replace("/", "_")
        path = out_dir / name
        path.write_bytes(result.content)
        saved.append({"url": target["url"], "path": str(path), "bytes": len(result.content)})
    return saved
