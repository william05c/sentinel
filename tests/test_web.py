"""The HTTP API, exercised without a network or a browser."""

from __future__ import annotations

import pytest

fastapi = pytest.importorskip("fastapi", reason="web extra not installed")
from fastapi.testclient import TestClient  # noqa: E402

from sentinel.config import Paths  # noqa: E402
from sentinel.web.app import create_app  # noqa: E402


@pytest.fixture()
def client(con, tmp_path):
    return TestClient(create_app(con, Paths.build(tmp_path)))


def test_health_reports_warehouse_counts(client):
    body = client.get("/api/health").json()
    assert body["status"] == "ok"
    assert body["companies"] >= 1


def test_company_search_finds_apple(client):
    rows = client.get("/api/companies", params={"q": "apple"}).json()
    assert rows and any("Apple" in row["name"] for row in rows)


def test_company_detail_has_metrics(client):
    body = client.get("/api/company/320193").json()
    assert body["company"]["tickers"] == "AAPL"
    assert body["metrics"], "a company page without metrics is not useful"
    metric = body["metrics"][0]
    assert {"metric_id", "value", "accession"} <= set(metric)


def test_unknown_company_is_404(client):
    assert client.get("/api/company/999999999").status_code == 404


def test_findings_endpoint_returns_a_list(client):
    assert isinstance(client.get("/api/findings").json(), list)


def test_digest_endpoint_answers_even_with_no_events(client):
    body = client.get("/api/digest").json()
    assert "quiet" in body and "items" in body


def test_crawl_without_the_browser_extra_is_503_with_instructions(client):
    response = client.post("/api/crawl", json={"url": "https://example.com"})
    if response.status_code == 503:
        assert "crawl4ai" in response.json()["detail"]
    else:  # the extra is installed in this environment
        assert response.status_code == 200


def test_index_links_the_pages(client):
    assert "SENTINEL API" in client.get("/").text
