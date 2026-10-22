"""The watch: parsing the change feed, scoring it, and staying quiet."""

from __future__ import annotations

from datetime import date, datetime
from pathlib import Path

import pandas as pd

from sentinel import SCHEMA_VERSION
from sentinel.adapters import sec_index
from sentinel.warehouse import db

FIXTURES = Path(__file__).parent / "fixtures"


def memory():
    con = db.connect(":memory:")
    con.execute(db.SCHEMA_PATH.read_text(encoding="utf-8"))
    return con


def test_parse_daily_index():
    frame = sec_index.parse_index((FIXTURES / "form_index_sample.idx").read_text(encoding="utf-8"))
    assert len(frame) > 100
    assert {"form_type", "cik", "filename", "accession"} <= set(frame.columns)
    assert frame["cik"].dtype.kind in "iu"
    # accession numbers are rebuilt from the archive path, not parsed from prose
    assert frame["accession"].dropna().str.match(r"^\d{10}-\d{2}-\d{6}$").all()


def test_accession_from_path():
    assert sec_index._accession_from("edgar/data/320193/000032019325000079/aapl-20250927.htm") == "0000320193-25-000079"
    assert sec_index._accession_from("nonsense") is None


def test_url_uses_quarter_path():
    assert sec_index.url_for(date(2024, 8, 1)).endswith("/2024/QTR3/form.20240801.idx")
    assert sec_index.url_for(date(2025, 1, 15)).endswith("/2025/QTR1/form.20250115.idx")


def test_materiality_weights():
    from sentinel.watch import score
    assert score("25-NSE")[0] > 0.9
    assert score("4")[0] < 0.2
    assert score("SOMETHING-NEW")[0] < 0.3


def test_digest_is_quiet_when_nothing_material():
    from sentinel import watch

    con = memory()
    watch.ensure_schema(con)
    d = watch.digest(con, day=date(2026, 1, 5))
    assert d["quiet"] is True
    assert d["items"] == []
    assert "Nothing material changed" in watch.render(d)


def test_digest_surfaces_and_suppresses():
    from sentinel import watch

    con = memory()
    watch.ensure_schema(con)
    rows = pd.DataFrame([
        {"accession": "A1", "event_class": "25-NSE", "cik": 1, "ticker": None, "form_type": "25-NSE",
         "filed_date": date(2026, 1, 5), "weight": 0.95, "reason": "delisting notice",
         "detected_at": datetime(2026, 1, 5, 12), "digest_date": date(2026, 1, 5),
         "surfaced": True, "schema_version": SCHEMA_VERSION},
        {"accession": "A2", "event_class": "4", "cik": 1, "ticker": None, "form_type": "4",
         "filed_date": date(2026, 1, 5), "weight": 0.12, "reason": "insider transaction",
         "detected_at": datetime(2026, 1, 5, 12), "digest_date": date(2026, 1, 5),
         "surfaced": False, "schema_version": SCHEMA_VERSION},
    ])
    db.replace_rows(con, "watch_event", rows, ["accession", "event_class"])

    d = watch.digest(con, day=date(2026, 1, 5))
    assert d["quiet"] is False
    assert len(d["items"]) == 1
    assert d["items"][0]["form_type"] == "25-NSE"
    assert d["suppressed"] == 1
    body = watch.render(d)
    assert "delisting notice" in body and "suppressed" in body

    # idempotent: re-recording the same filings must not duplicate events
    db.replace_rows(con, "watch_event", rows, ["accession", "event_class"])
    assert db.scalar(con, "SELECT COUNT(*) FROM watch_event") == 2
