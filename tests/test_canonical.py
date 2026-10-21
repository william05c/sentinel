"""Canonical selection: tag priority, point-in-time, restatements, fiscal calendars."""

from __future__ import annotations

from datetime import date, datetime, timezone

import pandas as pd

from sentinel import PARSER_VERSION, SCHEMA_VERSION
from sentinel.ingest import ensure_tag_map
from sentinel.normalize import canonical
from sentinel.warehouse import db

AS_OF_2020 = datetime(2020, 1, 1, tzinfo=timezone.utc)
AS_OF_2024 = datetime(2024, 1, 1, tzinfo=timezone.utc)
AS_OF_NOW = datetime(2026, 1, 1, tzinfo=timezone.utc)


def fact(tag: str, value: float, *, accn: str, accepted: datetime, start=None, end=date(2019, 9, 28)):
    return {
        "cik": 999999,
        "accession": accn,
        "taxonomy": "us-gaap",
        "tag": tag,
        "label": None,
        "unit": "USD",
        "value": value,
        "start_date": start,
        "end_date": end,
        "period_type": "duration" if start else "instant",
        "fiscal_year": 2019,
        "fiscal_period": "FY",
        "form": "10-K",
        "filed": accepted.date(),
        "accepted_at": accepted,
        "frame": None,
        "source_system": "test",
        "source_url": "https://example.invalid",
        "retrieved_at": datetime(2026, 1, 1),
        "document_sha256": None,
        "parser_version": PARSER_VERSION,
        "extractor_run_id": "test",
        "schema_version": SCHEMA_VERSION,
    }


def memory_db():
    con = db.connect(":memory:")
    con.execute((__import__("pathlib").Path("src/sentinel/warehouse/schema.sql")).read_text(encoding="utf-8"))
    ensure_tag_map(con)
    return con


def insert(con, rows):
    frame = pd.DataFrame(rows)
    con.register("_rows", frame)
    cols = ", ".join(frame.columns)
    con.execute("INSERT INTO fact_xbrl (" + cols + ") SELECT " + cols + " FROM _rows")
    con.unregister("_rows")


def test_tag_chain_priority_wins():
    """Revenue must come from the most preferred tag present, not the latest filing."""
    con = memory_db()
    start = date(2018, 9, 29)
    insert(con, [
        fact("Revenues", 100.0, accn="A-1", accepted=datetime(2019, 11, 1, tzinfo=timezone.utc), start=start),
        fact("RevenueFromContractWithCustomerExcludingAssessedTax", 200.0, accn="A-1",
             accepted=datetime(2019, 11, 1, tzinfo=timezone.utc), start=start),
    ])
    frame = canonical.build_canonical(con, 999999, AS_OF_NOW)
    revenue = frame[frame["metric_id"] == "revenue"].iloc[0]
    assert revenue["tag_used"] == "RevenueFromContractWithCustomerExcludingAssessedTax"
    assert revenue["value"] == 200.0
    assert revenue["n_candidates"] == 2


def test_point_in_time_excludes_later_filings(con):
    """A 2020 view must not contain a fact the SEC accepted in 2021."""
    frame = canonical.build_canonical(con, 320193, AS_OF_2020)
    assert not frame.empty
    accepted = pd.to_datetime(frame["accepted_at"])
    assert accepted.max() <= pd.Timestamp(AS_OF_2020.replace(tzinfo=None))
    years = pd.to_datetime(frame["end_date"]).dt.year
    assert years.max() <= 2020


def test_restatement_keeps_both_versions():
    con = memory_db()
    start = date(2018, 9, 29)
    insert(con, [
        fact("Revenues", 100.0, accn="ORIG", accepted=datetime(2019, 11, 1, tzinfo=timezone.utc), start=start),
        fact("Revenues", 120.0, accn="REST", accepted=datetime(2021, 3, 1, tzinfo=timezone.utc), start=start),
    ])
    before = canonical.build_canonical(con, 999999, AS_OF_2020)
    after = canonical.build_canonical(con, 999999, AS_OF_2024)
    assert before[before["metric_id"] == "revenue"].iloc[0]["value"] == 100.0
    assert before[before["metric_id"] == "revenue"].iloc[0]["accession"] == "ORIG"
    assert after[after["metric_id"] == "revenue"].iloc[0]["value"] == 120.0
    assert after[after["metric_id"] == "revenue"].iloc[0]["accession"] == "REST"
    # both versions survive in the fact table
    assert db.scalar(con, "SELECT COUNT(*) FROM fact_xbrl WHERE tag = 'Revenues'") == 2


def test_52_53_week_fiscal_year_is_annual():
    """A 364-day fiscal year must land in the annual bucket, not be dropped."""
    con = memory_db()
    insert(con, [fact("Revenues", 50.0, accn="A-1", accepted=datetime(2019, 11, 1, tzinfo=timezone.utc),
                      start=date(2018, 9, 30), end=date(2019, 9, 28))])
    frame = canonical.build_canonical(con, 999999, AS_OF_NOW)
    panel = canonical.annual_panel(frame)
    assert len(panel) == 1
    assert panel.iloc[0]["dur_days"] == 363
