"""The numeric gate: it must pass on a clean report and fail on a tampered one."""

from __future__ import annotations

from datetime import datetime, timezone

import pytest

from sentinel.config import Paths
from sentinel.reports import dossier
from sentinel.verify import find_unbound, verify_report

AS_OF = datetime(2026, 1, 1, tzinfo=timezone.utc)


@pytest.fixture()
def built(con, tmp_path):
    paths = Paths.build(tmp_path)
    paths.ensure()
    result = dossier.build(con, paths, 320193, as_of=AS_OF, years=5)
    return result, tmp_path


def test_clean_report_verifies(con, built):
    result, _ = built
    verification = verify_report(con, result["artifacts"]["markdown"])
    assert verification.ok, verification.summary()
    assert len(verification.checks) > 50


def test_tampered_number_fails(con, built):
    result, _ = built
    md_path = result["artifacts"]["markdown"]
    original = md_path.read_text(encoding="utf-8")
    bindings = result["bindings"]
    year_keys = [k for k in bindings if k.startswith("revenue_") and k.split("_")[1].isdigit()]
    key = max(year_keys, key=lambda k: int(k.split("_")[1]))
    rendered = bindings[key]["rendered"]
    tampered = original.replace(rendered, "$1.0B", 1)
    assert tampered != original
    md_path.write_text(tampered, encoding="utf-8")

    verification = verify_report(con, md_path)
    assert not verification.ok
    assert any(not c.ok and c.key == key for c in verification.checks)


def test_unbound_number_is_detected(con, built):
    result, _ = built
    md_path = result["artifacts"]["markdown"]
    md_path.write_text(
        md_path.read_text(encoding="utf-8") + "\nRevenue was 987654 dollars in the year.\n",
        encoding="utf-8",
    )
    verification = verify_report(con, md_path)
    assert not verification.ok
    assert "987654" in verification.unbound


def test_find_unbound_ignores_dates_and_accessions():
    text = (
        "As of 2024-05-01 the filing 0000320193-24-000123 showed revenue of "
        + "$391.0B" + " in FY2024.\n"
    )
    bindings = {"revenue": {"rendered": "$391.0B", "sql": "", "params": [], "fmt": "money"}}
    assert find_unbound(text, bindings) == []
