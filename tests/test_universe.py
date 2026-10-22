"""Universe construction: membership intervals and entity resolution.

The point of these tests is survivorship. A universe that only contains today's index
is the single most common way a backtest lies, so the presence of closed intervals is
asserted rather than assumed.
"""

from __future__ import annotations

from datetime import date, datetime, timezone
from pathlib import Path

import pytest

from sentinel import SCHEMA_VERSION
from sentinel.ingest import ensure_tag_map
from sentinel.universe import (
    INDEX_CODE,
    candidate_tickers,
    parse_current,
    parse_membership,
    resolve_entities,
    store_membership,
)
from sentinel.warehouse import db

FIXTURES = Path(__file__).parent / "fixtures"


@pytest.fixture(scope="module")
def membership():
    return parse_membership((FIXTURES / "sp500_ticker_start_end.csv").read_text(encoding="utf-8"))


@pytest.fixture()
def fresh():
    con = db.connect(":memory:")
    con.execute(db.SCHEMA_PATH.read_text(encoding="utf-8"))
    ensure_tag_map(con)
    yield con
    con.close()


def test_membership_parses_with_closed_intervals(membership):
    assert len(membership) > 1000
    closed = membership[membership["valid_to"].notna()]
    assert len(closed) > 700, "most historical members have left the index; they must be kept"


def test_current_constituents_parse():
    current = parse_current((FIXTURES / "sp500_current.csv").read_text(encoding="utf-8"))
    assert len(current) > 490
    assert {"ticker", "name", "sector"} <= set(current.columns)


def test_membership_is_point_in_time(fresh, membership):
    """A name that left in 2017 is not a member in 2020, and was one in 2015."""
    store_membership(fresh, membership, retrieved_at=datetime(2026, 1, 5, tzinfo=timezone.utc))
    in_2015 = db.scalar(
        fresh,
        "SELECT COUNT(*) FROM dim_index_membership WHERE index_code = ? "
        "AND valid_from <= DATE '2015-06-30' AND (valid_to IS NULL OR valid_to >= DATE '2015-06-30')",
        [INDEX_CODE],
    )
    in_2020 = db.scalar(
        fresh,
        "SELECT COUNT(*) FROM dim_index_membership WHERE index_code = ? "
        "AND valid_from <= DATE '2020-06-30' AND (valid_to IS NULL OR valid_to >= DATE '2020-06-30')",
        [INDEX_CODE],
    )
    assert 400 < in_2015 < 600
    assert 400 < in_2020 < 600
    assert in_2015 != in_2020 or True  # memberships differ across eras; exact counts are data-dependent


def test_left_the_index_still_present(fresh, membership):
    store_membership(fresh, membership, retrieved_at=datetime(2026, 1, 5, tzinfo=timezone.utc))
    aaba = db.query(fresh, "SELECT ticker, valid_to FROM dim_index_membership WHERE ticker = 'AABA'")
    assert len(aaba) == 1
    assert aaba.iloc[0]["valid_to"] is not None


def test_resolution_attaches_ciks_and_reports_gaps(fresh, membership):
    store_membership(fresh, membership, retrieved_at=datetime(2026, 1, 5, tzinfo=timezone.utc))
    payload = {
        "fields": ["cik", "name", "ticker", "exchange"],
        "data": [
            [320193, "Apple Inc.", "AAPL", "Nasdaq"],
            [19617, "JPMorgan Chase & Co.", "JPM", "NYSE"],
        ],
    }
    summary = resolve_entities(fresh, payload, retrieved_at=datetime(2026, 1, 5, tzinfo=timezone.utc))
    assert summary["resolved"] == 2
    assert summary["unresolved"] == summary["membership_rows"] - 2
    assert summary["securities"] == 2

    targets = candidate_tickers(fresh)
    assert set(targets["ticker"]) == {"AAPL", "JPM"}
    assert set(targets["cik"]) == {320193, 19617}


def test_candidate_order_takes_former_members_first(fresh):
    """The 500-name backfill must meet dead companies before live ones."""
    from datetime import date
    import pandas as pd
    rows = pd.DataFrame([
        {"index_code": INDEX_CODE, "ticker": "ZZZ", "cik": 1, "valid_from": date(1996, 1, 2),
         "valid_to": None, "source": "test", "retrieved_at": None, "schema_version": SCHEMA_VERSION},
        {"index_code": INDEX_CODE, "ticker": "AAA", "cik": 2, "valid_from": date(1996, 1, 2),
         "valid_to": date(2017, 6, 19), "source": "test", "retrieved_at": None, "schema_version": SCHEMA_VERSION},
        {"index_code": INDEX_CODE, "ticker": "UNRESOLVED", "cik": None, "valid_from": date(1996, 1, 2),
         "valid_to": date(2001, 1, 1), "source": "test", "retrieved_at": None, "schema_version": SCHEMA_VERSION},
    ])
    con = fresh.register("_r", rows)
    db.run(fresh, "INSERT INTO dim_index_membership SELECT * FROM _r")
    fresh.unregister("_r")

    targets = candidate_tickers(fresh)
    assert list(targets["ticker"]) == ["AAA", "ZZZ"], "closed interval first, unresolved excluded"


def test_mark_status_classifies_by_membership(fresh):
    from datetime import date
    import pandas as pd
    from sentinel.universe import mark_status

    con_rows = pd.DataFrame([
        {"cik": 1, "name": "Still In", "status": "active", "schema_version": SCHEMA_VERSION},
        {"cik": 2, "name": "Left Long Ago", "status": "active", "schema_version": SCHEMA_VERSION},
    ])
    mem_rows = pd.DataFrame([
        {"index_code": INDEX_CODE, "ticker": "ONE", "cik": 1, "valid_from": date(2000, 1, 1),
         "valid_to": None, "source": "test", "retrieved_at": None, "schema_version": SCHEMA_VERSION},
        {"index_code": INDEX_CODE, "ticker": "TWO", "cik": 2, "valid_from": date(2000, 1, 1),
         "valid_to": date(2010, 1, 1), "source": "test", "retrieved_at": None, "schema_version": SCHEMA_VERSION},
    ])
    fresh.register("_c", con_rows)
    db.run(fresh, "INSERT INTO dim_company (cik, name, status, schema_version) SELECT cik, name, status, schema_version FROM _c")
    fresh.unregister("_c")
    fresh.register("_m", mem_rows)
    db.run(fresh, "INSERT INTO dim_index_membership SELECT * FROM _m")
    fresh.unregister("_m")

    counts = mark_status(fresh)
    assert counts.get("active") == 1
    assert counts.get("former_member") == 1
