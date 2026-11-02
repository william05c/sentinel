"""Document ingestion: fetch a filing, extract its sections, score them, store them.

Sections are stored with their own provenance (accession + content hash) so a text
metric can always be traced back to the paragraph it came from.
"""

from __future__ import annotations

import hashlib
from datetime import datetime, timezone

import pandas as pd

from sentinel import PARSER_VERSION, SCHEMA_VERSION
from sentinel.adapters import filing_text
from sentinel.analytics import text as text_mod
from sentinel.warehouse import db

PRIMARY_URL = "https://www.sec.gov/Archives/edgar/data/{cik}/{accession}/{document}"


def latest_filing(con, cik: int, form: str = "10-K") -> dict | None:
    row = con.execute(
        "SELECT accession, primary_document, form_type, filed_date FROM fact_filing "
        "WHERE cik = ? AND form_type = ? AND primary_document IS NOT NULL "
        "ORDER BY filed_date DESC LIMIT 1",
        [cik, form],
    ).fetchall()
    if not row:
        return None
    return {"accession": row[0][0], "document": row[0][1], "form": row[0][2], "filed": row[0][3]}


def filing_url(cik: int, accession: str, document: str) -> str:
    return PRIMARY_URL.format(cik=cik, accession=accession.replace("-", ""), document=document)


def ingest_sections(con, fetcher, cik: int, *, form: str = "10-K", accession: str | None = None) -> dict:
    """Fetch one filing's primary document and store its sections."""
    target = latest_filing(con, cik, form) if accession is None else None
    if accession is not None:
        row = con.execute(
            "SELECT accession, primary_document, form_type, filed_date FROM fact_filing WHERE accession = ?",
            [accession],
        ).fetchall()
        if not row:
            raise LookupError("unknown accession " + accession)
        target = {"accession": row[0][0], "document": row[0][1], "form": row[0][2], "filed": row[0][3]}
    if not target:
        raise LookupError("no " + form + " with a primary document for cik " + str(cik))

    url = filing_url(cik, target["accession"], target["document"])
    result = fetcher.fetch(url)
    sections = filing_text.extract_sections(filing_text.to_text(result.content))
    if not sections:
        return {"accession": target["accession"], "sections": 0, "chars": 0, "url": url}

    now = datetime.now(timezone.utc).replace(tzinfo=None)
    rows = []
    for section in sections:
        tone = text_mod.tone(section.text)
        rows.append({
            "accession": target["accession"],
            "section_id": section.section_id,
            "section_name": section.name,
            "text": section.text,
            "char_count": section.char_count,
            "sentiment_pos": tone.positive,
            "sentiment_neg": tone.negative,
            "sentiment_unc": tone.uncertainty,
            "litigious": tone.litigious,
            "constraining": tone.constraining,
            "fog_index": tone.fog,
            "content_sha256": hashlib.sha256(section.text.encode("utf-8")).hexdigest(),
            "parser_version": PARSER_VERSION,
            "retrieved_at": result.fetched_at.replace(tzinfo=None),
            "schema_version": SCHEMA_VERSION,
        })
    db.run(con, "DELETE FROM dim_document_section WHERE accession = ?", [target["accession"]])
    db.replace_rows(con, "dim_document_section", pd.DataFrame(rows), ["accession", "section_id"])
    return {"accession": target["accession"], "sections": len(rows),
            "chars": sum(r["char_count"] for r in rows), "url": url}


def section_text(con, accession: str, section_id: str) -> str | None:
    row = con.execute(
        "SELECT text FROM dim_document_section WHERE accession = ? AND section_id = ?",
        [accession, section_id],
    ).fetchall()
    return row[0][0] if row else None
