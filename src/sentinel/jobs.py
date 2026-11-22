"""Crawl jobs: run it, watch it, keep what it found.

The consoles that ship with Crawlee, Crawl4AI and Firecrawl all agree on one shape: you
submit work, you watch status, you read the result, you take the output away. This module
is that shape for SENTINEL, backed by the warehouse rather than by process memory, so a
job survives a restart and its results are queryable afterwards.

A job is deliberately boring: it fetches a list of URLs through the polite fetcher,
records one row per URL, and writes files into data/files/<job_id>/. No scheduler, no
queue broker, one worker thread.
"""

from __future__ import annotations

import hashlib
import threading
import traceback
import zipfile
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pandas as pd

from sentinel import SCHEMA_VERSION
from sentinel.warehouse import db

MAX_URLS = 50

PRIVATE_PREFIXES = ("127.", "10.", "192.168.", "169.254.", "0.", "::1")
PRIVATE_NAMES = ("localhost", "localhost.localdomain", "metadata.google.internal")


class UnsafeTarget(ValueError):
    """A URL that would make this process fetch something it should not."""


def assert_safe_target(url: str) -> None:
    """Refuse loopback, link-local and private targets.

    The console accepts arbitrary URLs from whoever can reach it. Without this check the
    endpoint is a server-side request forgery primitive pointed at the host it runs on:
    cloud metadata, admin panels on 127.0.0.1, anything on the LAN.
    """
    from urllib.parse import urlparse

    parsed = urlparse(url)
    if parsed.scheme not in ("http", "https"):
        raise UnsafeTarget("only http and https are allowed: " + url[:60])
    host = (parsed.hostname or "").lower()
    if not host:
        raise UnsafeTarget("URL has no host: " + url[:60])
    if host in PRIVATE_NAMES or host.endswith(".local") or host.endswith(".internal"):
        raise UnsafeTarget("refusing to crawl a private host: " + host)
    if host.startswith(PRIVATE_PREFIXES):
        raise UnsafeTarget("refusing to crawl a private address: " + host)
    if host.startswith("172."):
        try:
            second = int(host.split(".")[1])
        except (IndexError, ValueError):
            second = 0
        if 16 <= second <= 31:
            raise UnsafeTarget("refusing to crawl a private address: " + host)


_timedelta = timedelta


def _now() -> datetime:
    return datetime.now(timezone.utc).replace(tzinfo=None)


def create(con, *, kind: str, target: str, total: int) -> str:
    job_id = "j-" + hashlib.sha1((kind + target + _now().isoformat()).encode()).hexdigest()[:12]
    db.run(con, "INSERT INTO crawl_job VALUES (?,?,?,?,?,?,?,?,?,?)",
           [job_id, _now(), kind, target[:120], "queued", total, 0, None, None, SCHEMA_VERSION])
    return job_id


def _clean(frame: pd.DataFrame) -> pd.DataFrame:
    """NULL comes back from DuckDB as NaN, and NaN is truthy in Python and JavaScript.

    A successful row with NaN in its error column therefore renders as a failure in the
    console and serialises to invalid JSON in the API. NULL has to be None before either
    consumer sees it.
    """
    as_object = frame.astype(object)
    return as_object.where(pd.notna(as_object), None)


def get(con, job_id: str) -> dict | None:
    rows = db.query(con, "SELECT * FROM crawl_job WHERE job_id = ?", [job_id])
    return None if rows.empty else _clean(rows).iloc[0].to_dict()


def listing(con, limit: int = 20) -> list[dict]:
    frame = db.query(con, "SELECT * FROM crawl_job ORDER BY created_at DESC LIMIT ?", [limit])
    return _clean(frame).to_dict("records")


def results(con, job_id: str) -> list[dict]:
    frame = db.query(con, "SELECT * FROM crawl_result WHERE job_id = ? ORDER BY seq", [job_id])
    return _clean(frame).to_dict("records")


def _set(con, job_id: str, **fields) -> None:
    columns = ", ".join(k + " = ?" for k in fields)
    db.run(con, "UPDATE crawl_job SET " + columns + " WHERE job_id = ?", list(fields.values()) + [job_id])


def run(con, paths, fetcher, job_id: str, urls: list[str]) -> None:
    """Fetch each URL, recording one row per attempt. Never raises: a crawl that dies
    halfway must still leave its partial results readable."""
    _set(con, job_id, status="running")
    out_dir = Path(paths.data) / "files" / job_id
    out_dir.mkdir(parents=True, exist_ok=True)
    done = 0
    try:
        for seq, url in enumerate(urls[:MAX_URLS]):
            row = {"job_id": job_id, "seq": seq, "url": url, "status": None, "bytes": None,
                   "title": None, "excerpt": None, "saved_path": None, "error": None,
                   "schema_version": SCHEMA_VERSION}
            try:
                result = fetcher.fetch(url)
                name = hashlib.sha1(url.encode()).hexdigest()[:10] + _suffix(url)
                path = out_dir / name
                path.write_bytes(result.content)
                row.update(status=result.status, bytes=len(result.content),
                           saved_path=str(path), title=url.rsplit("/", 1)[-1])
                row["excerpt"] = _excerpt(result.content)
            except Exception as exc:
                row["error"] = type(exc).__name__ + ": " + str(exc)[:160]
            db.run(con, "INSERT OR REPLACE INTO crawl_result VALUES (?,?,?,?,?,?,?,?,?,?)",
                   [row["job_id"], row["seq"], row["url"], row["status"], row["bytes"],
                    row["title"], row["excerpt"], row["saved_path"], row["error"], row["schema_version"]])
            done += 1
            _set(con, job_id, done=done)
        _set(con, job_id, status="done", finished_at=_now())
    except Exception:
        _set(con, job_id, status="failed", error=traceback.format_exc()[-400:], finished_at=_now())


def reconcile_stale(con, *, minutes: int = 15, now: datetime | None = None) -> int:
    """Mark jobs abandoned by a dead process, so `running` cannot mean `rotting`.

    A job whose worker died - process killed, machine rebooted, exception outside the
    guarded loop - otherwise reads as running forever and the console shows a spinner
    for something that stopped existing.
    """
    reference = now or _now()
    cutoff = reference - _timedelta(minutes=minutes)
    rows = db.query(con, "SELECT job_id FROM crawl_job WHERE status IN ('running','queued') AND created_at < ?",
                    [cutoff])
    for job_id in rows["job_id"].tolist():
        _set(con, job_id, status="failed", finished_at=reference,
             error="stale: no progress for " + str(minutes) + " minutes (worker stopped)")
    return len(rows)


def start(con_factory, paths, fetcher, job_id: str, urls: list[str]) -> None:
    """Run in a daemon thread so the request can return immediately.

    The thread opens its own connection: DuckDB connections are not thread-safe and the
    request's connection is closed when the response is sent.
    """
    def worker():
        connection = con_factory()
        try:
            run(connection, paths, fetcher, job_id, urls)
        finally:
            connection.close()

    threading.Thread(target=worker, name="crawl-" + job_id, daemon=True).start()


def bundle(con, paths, job_id: str) -> Path | None:
    """Zip the saved files plus a manifest, for one-click download."""
    rows = results(con, job_id)
    if not rows:
        return None
    out = Path(paths.data) / "files" / (job_id + ".zip")
    import csv
    import io

    buffer = io.StringIO()
    writer = csv.writer(buffer)
    writer.writerow(["seq", "url", "status", "bytes", "error"])
    for row in rows:
        # csv.writer, not f-strings: a URL containing a quote corrupted the manifest.
        writer.writerow([row["seq"], row["url"], row["status"] or "", row["bytes"] or "", row["error"] or ""])
    with zipfile.ZipFile(out, "w", zipfile.ZIP_DEFLATED) as zf:
        zf.writestr("manifest.csv", buffer.getvalue())
        for row in rows:
            saved = row.get("saved_path")
            if saved and Path(saved).exists():
                zf.write(saved, arcname=Path(saved).name)
    return out


def _suffix(url: str) -> str:
    tail = url.rsplit("/", 1)[-1].lower()
    for ext in (".htm", ".html", ".json", ".xml", ".txt", ".zip", ".pdf", ".csv"):
        if tail.endswith(ext):
            return ext
    return ".bin"


def _excerpt(content: bytes, limit: int = 240) -> str:
    try:
        text = content.decode("utf-8", errors="ignore")
    except Exception:
        return ""
    import re
    text = re.sub(r"<[^>]+>", " ", text)
    return " ".join(text.split())[:limit]
