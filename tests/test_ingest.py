"""Ingest behaviour: rows written, and identical results on a second run."""

from __future__ import annotations

from sentinel.warehouse import db


def test_ingest_writes_company_and_filings(con):
    company = db.query(con, "SELECT name, sic, tickers FROM dim_company WHERE cik = 320193")
    assert len(company) == 1
    assert company.iloc[0]["name"] == "Apple Inc."
    assert company.iloc[0]["tickers"] == "AAPL"

    filings = db.scalar(con, "SELECT COUNT(*) FROM fact_filing WHERE cik = 320193")
    assert filings > 900, "expected the full recent submissions window"

    facts = db.scalar(con, "SELECT COUNT(*) FROM fact_xbrl WHERE cik = 320193")
    assert facts > 1000

    canonical = db.scalar(con, "SELECT COUNT(*) FROM fact_xbrl_canonical WHERE cik = 320193")
    assert canonical > 100


def test_ingest_is_idempotent(warehouse, fetcher):
    """Running the same ingest twice must not duplicate rows."""
    before = db.connect(warehouse)
    counts_before = db.table_counts(before)
    digest_before = before.execute(
        "SELECT COUNT(*), SUM(value), SUM(LENGTH(COALESCE(accession, ''))) FROM fact_xbrl"
    ).fetchone()
    before.close()

    con = db.connect(warehouse)
    from sentinel.ingest import ingest_company
    ingest_company(con, fetcher, 320193)
    con.close()

    after = db.connect(warehouse)
    counts_after = db.table_counts(after)
    digest_after = after.execute(
        "SELECT COUNT(*), SUM(value), SUM(LENGTH(COALESCE(accession, ''))) FROM fact_xbrl"
    ).fetchone()
    after.close()

    assert counts_before["fact_xbrl"] == counts_after["fact_xbrl"]
    assert counts_before["fact_filing"] == counts_after["fact_filing"]
    assert digest_before == digest_after


def test_every_fact_has_provenance(con):
    missing = db.scalar(
        con,
        "SELECT COUNT(*) FROM fact_xbrl WHERE source_system IS NULL OR source_url IS NULL "
        "OR retrieved_at IS NULL OR accepted_at IS NULL OR parser_version IS NULL",
    )
    assert missing == 0
