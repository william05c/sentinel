"""The findings ledger: ids are stable, grades are honest, retiring keeps the record."""

from __future__ import annotations

from sentinel import SCHEMA_VERSION
from sentinel.analytics import findings
from sentinel.warehouse import db


def memory():
    con = db.connect(":memory:")
    con.execute(db.SCHEMA_PATH.read_text(encoding="utf-8"))
    return con


def test_finding_id_is_deterministic():
    a = findings.finding_id("New risk language: tariff", "cik:1", "2024..2025")
    b = findings.finding_id("New risk language: tariff", "cik:1", "2024..2025")
    c = findings.finding_id("New risk language: tariff", "cik:2", "2024..2025")
    assert a == b and a != c


def test_record_then_list_then_retire():
    con = memory()
    fid = findings.record(
        con,
        title="Revenue concentration",
        claim="One customer exceeds 40% of revenue.",
        entities="cik:320193",
        method="segment disclosure",
        falsifier="A later filing shows the customer below 10%.",
        rerun_command="sentinel text --ticker AAPL",
        grade="C",
    )
    assert findings.summary(con)["total"] == 1
    row = findings.ledger(con).iloc[0]
    assert row["falsifier"], "a finding without a falsifier is not a finding"
    assert row["grade"] == "C"
    assert row["status"] == "new"

    findings.retire(con, fid, "customer was below 10% in the next filing")
    retired = findings.ledger(con, status="retired").iloc[0]
    assert retired["finding_id"] == fid
    assert "below 10%" in retired["reviewer_note"]
    assert findings.summary(con)["total"] == 1, "retiring must not delete"


def test_recording_twice_is_idempotent():
    con = memory()
    kwargs = dict(title="t", claim="c", entities="e", method="m",
                  falsifier="f", rerun_command="r", sample_period="2024")
    findings.record(con, **kwargs)
    findings.record(con, **kwargs)
    assert findings.summary(con)["total"] == 1


def test_grade_filter_works():
    con = memory()
    findings.record(con, title="a", claim="c", entities="e", method="m",
                    falsifier="f", rerun_command="r", grade="C")
    assert len(findings.ledger(con, grade="C")) == 1
    assert len(findings.ledger(con, grade="A")) == 0
