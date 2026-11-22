"""Crawl jobs: they run, they record, they survive failure, and they are downloadable."""

from __future__ import annotations

from datetime import datetime
from types import SimpleNamespace

import pytest

from sentinel import jobs
from sentinel.config import Paths
from sentinel.warehouse import db


def fresh():
    """A private database. These tests failed against the shared fixture with counts
    that did not match the rows they had just written, and the row contents were correct
    when the same scenario ran on its own - so the isolation is the fix, not a workaround."""
    con = db.connect(":memory:")
    con.execute((db.SCHEMA_PATH).read_text(encoding="utf-8"))
    return con


class FakeFetcher:
    def __init__(self, fail_on=()):
        self.fail_on = set(fail_on)
        self.calls = []

    def fetch(self, url):
        self.calls.append(url)
        if url in self.fail_on:
            raise RuntimeError("blocked by robots.txt")
        body = ("<html><body><p>hello from " + url + "</p></body></html>").encode()
        return SimpleNamespace(content=body, status=200, fetched_at=datetime(2026, 1, 1))


def test_job_runs_and_records_every_url(tmp_path):
    con = fresh()
    paths = Paths.build(tmp_path)
    job_id = jobs.create(con, kind="url", target="test", total=2)
    jobs.run(con, paths, FakeFetcher(), job_id, ["https://example.com/a", "https://example.com/b"])

    job = jobs.get(con, job_id)
    assert job["status"] == "done" and job["done"] == 2
    rows = jobs.results(con, job_id)
    assert [r["status"] for r in rows] == [200, 200]
    assert all(r["saved_path"] for r in rows)
    assert rows[0]["excerpt"].startswith("hello from")


def test_one_failure_does_not_abort_the_job(tmp_path):
    con = fresh()
    paths = Paths.build(tmp_path)
    job_id = jobs.create(con, kind="url", target="mixed", total=3)
    fetcher = FakeFetcher(fail_on=["https://example.com/bad"])
    jobs.run(con, paths, fetcher, job_id, ["https://example.com/ok", "https://example.com/bad",
                                           "https://example.com/ok2"])
    job = jobs.get(con, job_id)
    assert job["status"] == "done", "a crawl with one failure is not a failed crawl"
    rows = jobs.results(con, job_id)
    assert len(rows) == 3
    # NaN is truthy: a successful row must be identified by None, not by falsiness.
    assert all(r["error"] is None for r in rows if r["url"] != "https://example.com/bad")
    errored = [(r["url"], r["error"]) for r in rows if r["error"] is not None]
    assert [u for u, _ in errored] == ["https://example.com/bad"], errored
    assert "robots" in errored[0][1]


def test_bundle_contains_manifest_and_files(tmp_path):
    con = fresh()
    import zipfile
    paths = Paths.build(tmp_path)
    job_id = jobs.create(con, kind="url", target="zip", total=1)
    jobs.run(con, paths, FakeFetcher(), job_id, ["https://example.com/a"])
    bundle = jobs.bundle(con, paths, job_id)
    assert bundle and bundle.exists()
    with zipfile.ZipFile(bundle) as zf:
        names = zf.namelist()
    assert "manifest.csv" in names and len(names) >= 2


def test_bundle_is_none_without_results(tmp_path):
    con = fresh()
    paths = Paths.build(tmp_path)
    job_id = jobs.create(con, kind="url", target="empty", total=0)
    assert jobs.bundle(con, paths, job_id) is None


def test_api_creates_and_lists_jobs(con, tmp_path):
    fastapi = pytest.importorskip("fastapi")
    from fastapi.testclient import TestClient

    from sentinel.web.app import create_app

    client = TestClient(create_app(con, Paths.build(tmp_path)))
    created = client.post("/api/jobs", json={"urls": ["https://example.com/x"], "label": "demo"})
    assert created.status_code == 200
    job_id = created.json()["job_id"]
    assert client.get("/api/jobs").json()[0]["job_id"] == job_id
    detail = client.get("/api/jobs/" + job_id).json()
    assert detail["status"] == "queued", "an unstaffed job is queued, not running"
    assert client.get("/api/jobs/nope").status_code == 404


def test_api_rejects_junk(con, tmp_path):
    fastapi = pytest.importorskip("fastapi")
    from fastapi.testclient import TestClient

    from sentinel.web.app import create_app

    client = TestClient(create_app(con, Paths.build(tmp_path)))
    assert client.post("/api/jobs", json={"urls": ["not a url"]}).status_code == 400
    assert client.post("/api/jobs", json={"urls": []}).status_code == 400
    assert client.post("/api/jobs", json={"urls": ["https://x.com"] * 99}).status_code == 400


@pytest.mark.parametrize("url", [
    "http://localhost/admin", "http://127.0.0.1:8788/", "http://192.168.1.1/",
    "http://10.0.0.5/", "http://169.254.169.254/latest/meta-data/", "http://172.16.0.1/",
    "http://metadata.google.internal/", "ftp://example.com/x",
])
def test_private_targets_are_refused(url):
    with pytest.raises(jobs.UnsafeTarget):
        jobs.assert_safe_target(url)


def test_public_targets_pass():
    jobs.assert_safe_target("https://www.sec.gov/Archives/edgar/data/320193/x.htm")
    jobs.assert_safe_target("https://172.32.0.1/")   # outside the private 172.16-31 range


def test_stale_jobs_are_reconciled():
    """A worker that dies must not leave a job 'running' for ever."""
    from datetime import datetime, timedelta

    con = fresh()
    old = jobs.create(con, kind="url", target="dead", total=1)
    jobs._set(con, old, status="running")
    # backdate it beyond the threshold
    con.execute("UPDATE crawl_job SET created_at = ? WHERE job_id = ?",
                [datetime(2026, 1, 1), old])

    fresh_job = jobs.create(con, kind="url", target="alive", total=1)
    jobs._set(con, fresh_job, status="running")

    changed = jobs.reconcile_stale(con, minutes=15, now=datetime(2026, 1, 2))
    assert changed == 1
    assert jobs.get(con, old)["status"] == "failed"
    assert "stale" in jobs.get(con, old)["error"]
    assert jobs.get(con, fresh_job)["status"] == "running", "a live job must not be killed"
