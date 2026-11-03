"""Targets and the optional clients: the manifest must point at real files."""

from __future__ import annotations

import json

import pytest

from sentinel import targets
from sentinel.config import Paths


def test_manifest_lists_real_documents(con, tmp_path):
    paths = Paths.build(tmp_path)
    manifest = targets.build_manifest(con, paths, ticker="AAPL", forms=["10-K"], limit=5)
    assert manifest["counts"]["filing"] >= 1
    filings = [x for x in manifest["targets"] if x["kind"] == "filing"]
    assert filings, "expected at least one filing document"
    for target in filings:
        assert target["url"].startswith("https://www.sec.gov/Archives/edgar/data/")
        assert target["document"], "a target without a document name is not a target"
    assert (tmp_path / "viz" / "targets.json").exists()
    saved = json.loads((tmp_path / "viz" / "targets.json").read_text(encoding="utf-8"))
    assert saved["company"]["cik"] == 320193


def test_bulk_targets_point_at_quarterly_archives():
    rows = targets.bulk_targets(years_back=1)
    assert len(rows) == 4
    assert all(row["url"].endswith(".zip") for row in rows)
    assert all(row["kind"] == "bulk" for row in rows)


def test_unknown_ticker_raises(con, tmp_path):
    with pytest.raises(LookupError):
        targets.build_manifest(con, Paths.build(tmp_path), ticker="NOTATICKER")


def test_firecrawl_is_off_unless_configured(monkeypatch):
    from sentinel.adapters import firecrawl

    monkeypatch.delenv("FIRECRAWL_API_KEY", raising=False)
    monkeypatch.delenv("FIRECRAWL_API_URL", raising=False)
    assert firecrawl.available() is False
    monkeypatch.setenv("FIRECRAWL_API_KEY", "fc-test")
    assert firecrawl.available() is True
    # base url honours a self-hosted instance
    monkeypatch.setenv("FIRECRAWL_API_URL", "http://localhost:3002/")
    assert firecrawl.base_url() == "http://localhost:3002"


def test_browser_backend_reports_its_own_absence():
    from sentinel.crawler import browser

    if not browser.available():
        with pytest.raises(ImportError) as err:
            browser.render_sync("https://example.com")
        assert "playwright install" in str(err.value)
