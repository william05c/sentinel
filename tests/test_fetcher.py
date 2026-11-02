"""Fetcher behaviour against a mocked transport: robots, 304, budget."""

from __future__ import annotations

from datetime import datetime, timezone

import httpx
import pytest

from sentinel.config import Paths, identity, sources_config
from sentinel.crawler.net import Budget, HttpFetcher, SourceError


def make_fetcher(tmp_path, handler):
    paths = Paths.build(tmp_path)
    paths.ensure()
    _, defaults = sources_config()
    transport = httpx.MockTransport(handler)
    return HttpFetcher(paths, identity(), defaults, transport=transport, sleeper=lambda s: None)


def test_robots_disallow_is_obeyed(tmp_path):
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/robots.txt":
            return httpx.Response(200, text="User-agent: *\nDisallow: /secret\n")
        if request.url.path.startswith("/secret"):
            return httpx.Response(200, text="should never be fetched")
        return httpx.Response(200, text="ok", headers={"ETag": '"v1"'})

    fetcher = make_fetcher(tmp_path, handler)
    assert fetcher.fetch("https://www.sec.gov/open.json").status == 200
    with pytest.raises(SourceError) as err:
        fetcher.fetch("https://www.sec.gov/secret/page")
    assert "robots" in str(err.value)
    fetcher.close()


def test_unreachable_robots_blocks_everything(tmp_path):
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/robots.txt":
            return httpx.Response(503)
        return httpx.Response(200, text="ok")

    fetcher = make_fetcher(tmp_path, handler)
    with pytest.raises(SourceError):
        fetcher.fetch("https://www.sec.gov/anything")
    fetcher.close()


def test_conditional_request_reuses_body_on_304(tmp_path):
    calls = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/robots.txt":
            return httpx.Response(404)
        calls["n"] += 1
        if calls["n"] == 1:
            return httpx.Response(200, text="payload-v1", headers={"ETag": '"v1"'})
        assert request.headers.get("if-none-match") == '"v1"', "revalidation must send the ETag"
        return httpx.Response(304)

    fetcher = make_fetcher(tmp_path, handler)
    first = fetcher.fetch("https://www.sec.gov/doc.json", ttl_s=0)
    second = fetcher.fetch("https://www.sec.gov/doc.json", ttl_s=0)
    assert first.text == "payload-v1"
    assert second.text == "payload-v1"
    assert second.from_cache is True, "a 304 must be served from cache, not re-parsed from an empty body"
    fetcher.close()


def test_budget_ceiling_is_enforced(tmp_path):
    paths = Paths.build(tmp_path)
    paths.ensure()
    budget = Budget(paths.data / "crawl_budget.json")
    spec = sources_config()[0]["specs"]["sec_www"]

    budget.check("sec_www", spec)
    budget.record("sec_www", spec.max_bytes_per_day)   # spend the whole byte allowance
    with pytest.raises(SourceError):
        budget.check("sec_www", spec)


def test_budget_survives_a_restart(tmp_path):
    paths = Paths.build(tmp_path)
    paths.ensure()
    first = Budget(paths.data / "crawl_budget.json")
    first.record("sec_www", 100)
    second = Budget(paths.data / "crawl_budget.json")
    assert second._state["bytes"]["sec_www"] == 100
