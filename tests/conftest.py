"""Offline fixtures.

Nothing in this suite touches the network: the SEC payloads were captured once and
committed, and the parsers run over them exactly as they do in production.

The warehouse is IN MEMORY, module-scoped, with a fresh cursor per test. A file-backed
DuckDB was tried first and produced two distinct failures that had nothing to do with
the code: a transient file lock on a CI runner, and ResourceInUseException when one
test's statement was still open while another began. Tests should not be able to fail
for reasons unrelated to the behaviour under test.
"""

from __future__ import annotations

import gzip
import hashlib
import json
from datetime import datetime, timezone
from pathlib import Path

import pytest

from sentinel.crawler.net import FetchResult
from sentinel.ingest import ensure_tag_map, ingest_company
from sentinel.warehouse import db

FIXTURES = Path(__file__).parent / "fixtures"
CIK = 320193


def load(name: str) -> dict:
    plain = FIXTURES / name
    if plain.exists():
        return json.loads(plain.read_text(encoding="utf-8"))
    with gzip.open(FIXTURES / (name + ".gz"), "rb") as fh:
        return json.loads(fh.read())


class FakeFetcher:
    """Stands in for HttpFetcher, returning recorded payloads by URL."""

    def __init__(self, mapping: dict[str, dict]):
        self.mapping = mapping
        self.calls: list[str] = []

    def get_json(self, url: str, **kwargs):
        if url not in self.mapping:
            raise KeyError("no fixture for " + url)
        self.calls.append(url)
        payload = self.mapping[url]
        body = json.dumps(payload).encode("utf-8")
        result = FetchResult(
            url=url,
            status=200,
            content=body,
            sha256=hashlib.sha256(body).hexdigest(),
            fetched_at=datetime(2026, 1, 5, tzinfo=timezone.utc),
            from_cache=True,
            content_type="application/json",
            elapsed_ms=0,
        )
        return payload, result


@pytest.fixture(scope="session")
def submissions() -> dict:
    return load("aapl_submissions.json")


@pytest.fixture(scope="session")
def companyfacts() -> dict:
    return load("aapl_companyfacts.json")


@pytest.fixture(scope="session")
def fetcher(submissions, companyfacts) -> FakeFetcher:
    return FakeFetcher({
        f"https://data.sec.gov/submissions/CIK{CIK:010d}.json": submissions,
        f"https://data.sec.gov/api/xbrl/companyfacts/CIK{CIK:010d}.json": companyfacts,
    })


@pytest.fixture(scope="module")
def warehouse(fetcher):
    """One in-memory warehouse per test module, ingested once."""
    connection = db.connect(":memory:")
    connection.execute(db.SCHEMA_PATH.read_text(encoding="utf-8"))
    ensure_tag_map(connection)
    ingest_company(connection, fetcher, CIK)
    yield connection
    connection.close()


@pytest.fixture()
def con(warehouse):
    """A cursor gives each test its own handle on the shared in-memory database."""
    cursor = warehouse.cursor()
    yield cursor
    cursor.close()
