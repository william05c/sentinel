"""Restatements: a changed number is a finding, not an update."""

from __future__ import annotations

from datetime import date, datetime

import pandas as pd

from sentinel import SCHEMA_VERSION
from sentinel.ingest import ensure_tag_map
from sentinel.normalize import restatements
from sentinel.warehouse import db


def fact(value, accession, accepted):
    return {
        "cik": 42, "accession": accession, "taxonomy": "us-gaap", "tag": "Revenues",
        "label": None, "unit": "USD", "value": value,
        "start_date": date(2019, 1, 1), "end_date": date(2019, 12, 31),
        "period_type": "duration", "fiscal_year": 2019, "fiscal_period": "FY",
        "form": "10-K", "filed": accepted.date(), "accepted_at": accepted,
        "frame": None, "source_system": "test", "source_url": "https://example.invalid",
        "retrieved_at": datetime(2026, 1, 1), "document_sha256": None,
        "parser_version": "test", "extractor_run_id": "test",
        "schema_version": SCHEMA_VERSION,
    }


def memory():
    con = db.connect(":memory:")
    con.execute(db.SCHEMA_PATH.read_text(encoding="utf-8"))
    ensure_tag_map(con)
    return con


def insert(con, rows):
    frame = pd.DataFrame(rows)
    con.register("_r", frame)
    cols = ", ".join(frame.columns)
    db.run(con, "INSERT INTO fact_xbrl (" + cols + ") SELECT " + cols + " FROM _r")
    con.unregister("_r")


def test_restatement_is_detected_with_direction_and_size():
    con = memory()
    insert(con, [
        fact(100.0, "ORIG", datetime(2020, 2, 1)),
        fact(120.0, "REST", datetime(2021, 5, 1)),
    ])
    frame = restatements.detect(con)
    assert len(frame) == 1
    row = frame.iloc[0]
    assert row["prior_value"] == 100.0
    assert row["value"] == 120.0
    assert row["delta"] == 20.0
    assert abs(row["pct_change"] - 0.2) < 1e-9
    assert row["prior_accession"] == "ORIG"

    summary = restatements.summary(con)
    assert summary["value_changes"] == 1
    assert summary["scale_changes"] == 0
    assert summary["companies_affected"] == 1


def test_unchanged_values_are_not_restatements():
    con = memory()
    insert(con, [
        fact(100.0, "A", datetime(2020, 2, 1)),
        fact(100.0, "B", datetime(2021, 5, 1)),
    ])
    assert restatements.detect(con).empty
