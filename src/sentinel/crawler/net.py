"""The only path to the network.

Design notes that matter more than they look:

* The rate limiter is keyed by host and calibrated from config/sources.yaml, so a
  new source cannot accidentally inherit "as fast as possible".
* Bytes are written once, content-addressed by SHA-256, and never overwritten.
  Re-parsing therefore never needs the network, which is what makes a report
  re-runnable a year later.
* A 403 from SEC is ambiguous: it means either "you are too fast" or "that path
  does not exist". Both cases are retried a couple of times and then surfaced with
  the URL attached, because silently treating it as an empty result is how a
  pipeline ends up with holes that look like zeros.
"""

from __future__ import annotations

import gzip
import hashlib
import json
import random
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable
from urllib.parse import urlparse

import httpx

from sentinel.config import Defaults, Identity, Paths, SourceSpec, source_for_host

RETRY_STATUSES = {403, 408, 429, 500, 502, 503, 504}


class SourceError(RuntimeError):
    """A source failed in a way the caller must know about rather than paper over."""

    def __init__(self, url: str, status: int | None, message: str):
        self.url = url
        self.status = status
        super().__init__(f"{message} (url={url} status={status})")


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


@dataclass
class FetchResult:
    url: str
    status: int
    content: bytes
    sha256: str
    fetched_at: datetime
    from_cache: bool
    content_type: str
    elapsed_ms: int

    @property
    def text(self) -> str:
        return self.content.decode("utf-8", errors="replace")

    def json(self) -> Any:
        return json.loads(self.content)


@dataclass
class _HostState:
    last_request: float = 0.0
    consecutive_failures: int = 0
    open_until: float = 0.0


class RateLimiter:
    """One token bucket per host.

    A minimum inter-request interval is used rather than a sliding window: it is
    trivially correct, survives process restarts badly (resets), and errs slow,
    which is the failure mode we want.
    """

    def __init__(self, clock: Callable[[], float] = time.monotonic,
                 sleeper: Callable[[float], None] = time.sleep):
        self._clock = clock
        self._sleep = sleeper
        self._hosts: dict[str, _HostState] = {}

    def acquire(self, host: str, spec: SourceSpec | None) -> None:
        state = self._hosts.setdefault(host, _HostState())
        now = self._clock()
        if state.open_until > now:
            raise SourceError(f"https://{host}/", None,
                              f"circuit open for {state.open_until - now:.1f}s")
        if spec is None or spec.requests_per_second <= 0:
            interval = 1.0
        else:
            interval = 1.0 / spec.requests_per_second
        wait = state.last_request + interval - now
        if wait > 0:
            self._sleep(wait)
        state.last_request = self._clock()

    def note_success(self, host: str) -> None:
        state = self._hosts.setdefault(host, _HostState())
        state.consecutive_failures = 0

    def note_failure(self, host: str, threshold: int = 8, cooldown_s: float = 60.0) -> None:
        state = self._hosts.setdefault(host, _HostState())
        state.consecutive_failures += 1
        if state.consecutive_failures >= threshold:
            state.open_until = self._clock() + cooldown_s


class RawStore:
    """Content-addressed blobs plus a small JSON index of url -> hash."""

    def __init__(self, paths: Paths):
        self.paths = paths
        self._index: dict[str, dict] = {}
        if paths.cache_index.exists():
            try:
                self._index = json.loads(paths.cache_index.read_text(encoding="utf-8"))
            except json.JSONDecodeError:
                self._index = {}

    def lookup(self, url: str) -> dict | None:
        return self._index.get(url)

    def is_fresh(self, url: str, ttl_s: int) -> bool:
        entry = self.lookup(url)
        if not entry or not entry.get("sha256"):
            return False
        age = utcnow().timestamp() - datetime.fromisoformat(entry["fetched_at"]).timestamp()
        return age <= ttl_s

    def read(self, sha: str) -> bytes:
        host = sha[:2]
        path = self.paths.raw / host / f"{sha}.gz"
        with gzip.open(path, "rb") as fh:
            return fh.read()

    def write(self, url: str, content: bytes, status: int, content_type: str) -> str:
        sha = hashlib.sha256(content).hexdigest()
        path = self.paths.raw / sha[:2] / f"{sha}.gz"
        path.parent.mkdir(parents=True, exist_ok=True)
        if not path.exists():  # never overwrite: content-addressed means immutable
            with gzip.open(path, "wb") as fh:
                fh.write(content)
        self._index[url] = {
            "sha256": sha,
            "fetched_at": utcnow().isoformat(),
            "status": status,
            "content_type": content_type,
            "bytes": len(content),
        }
        self.flush()
        return sha

    def flush(self) -> None:
        self.paths.cache_index.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.paths.cache_index.with_suffix(".tmp")
        tmp.write_text(json.dumps(self._index, indent=0), encoding="utf-8")
        tmp.replace(self.paths.cache_index)


class HttpFetcher:
    def __init__(
        self,
        paths: Paths,
        identity: Identity,
        defaults: Defaults,
        *,
        transport: httpx.BaseTransport | None = None,
        sleeper: Callable[[float], None] = time.sleep,
        clock: Callable[[], float] = time.monotonic,
        logger: Callable[[dict], None] | None = None,
    ):
        self.paths = paths
        self.identity = identity
        self.defaults = defaults
        self.store = RawStore(paths)
        self.limiter = RateLimiter(clock=clock, sleeper=sleeper)
        self._client = httpx.Client(
            timeout=defaults.timeout_s,
            follow_redirects=True,
            transport=transport,
            headers={"User-Agent": identity.header, "Accept-Encoding": "gzip, deflate"},
        )
        self._logger = logger or self._default_log

    def _default_log(self, record: dict) -> None:
        self.paths.crawl_log.parent.mkdir(parents=True, exist_ok=True)
        with self.paths.crawl_log.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(record) + "\n")

    def close(self) -> None:
        self._client.close()

    def __enter__(self) -> HttpFetcher:
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()

    def fetch(self, url: str, *, ttl_s: int | None = None, allow_cache: bool = True) -> FetchResult:
        parsed = urlparse(url)
        host = parsed.netloc
        spec = source_for_host(host)
        if spec is not None and not spec.enabled:
            raise SourceError(url, None, f"source '{spec.name}' is disabled in config/sources.yaml")
        ttl = spec.cache_ttl_s if ttl_s is None else ttl_s

        if allow_cache and ttl > 0 and self.store.is_fresh(url, ttl):
            entry = self.store.lookup(url)
            assert entry is not None
            content = self.store.read(entry["sha256"])
            self._logger({"ts": utcnow().isoformat(), "url": url, "status": entry.get("status", 200),
                          "bytes": len(content), "ms": 0, "attempt": 0, "cache_hit": True,
                          "user_agent": self.identity.header})
            return FetchResult(url, int(entry.get("status", 200)), content, entry["sha256"],
                               datetime.fromisoformat(entry["fetched_at"]), True,
                               entry.get("content_type", ""), 0)

        last_error: Exception | None = None
        for attempt in range(1, self.defaults.max_retries + 1):
            self.limiter.acquire(host, spec)
            started = time.perf_counter()
            try:
                response = self._client.get(url)
            except httpx.HTTPError as exc:  # network-level failure
                last_error = exc
                self.limiter.note_failure(host)
                self._sleep_backoff(attempt)
                continue

            elapsed_ms = int((time.perf_counter() - started) * 1000)
            self._logger({"ts": utcnow().isoformat(), "url": url, "status": response.status_code,
                          "bytes": len(response.content), "ms": elapsed_ms, "attempt": attempt,
                          "cache_hit": False, "user_agent": self.identity.header})

            if response.status_code < 400:
                sha = self.store.write(url, response.content, response.status_code,
                                       response.headers.get("content-type", ""))
                self.limiter.note_success(host)
                return FetchResult(url, response.status_code, response.content, sha, utcnow(),
                                   False, response.headers.get("content-type", ""), elapsed_ms)

            if response.status_code not in RETRY_STATUSES:
                raise SourceError(url, response.status_code, "non-retryable HTTP status")

            last_error = SourceError(url, response.status_code, "retryable HTTP status")
            self.limiter.note_failure(host)
            self._sleep_backoff(attempt, retry_after=response.headers.get("Retry-After"))

        raise SourceError(url, None, f"exhausted {self.defaults.max_retries} attempts: {last_error}")

    def _sleep_backoff(self, attempt: int, retry_after: str | None = None) -> None:
        if retry_after:
            try:
                delay = float(retry_after)
            except ValueError:
                delay = 0.0
            if delay > 0:
                time.sleep(min(delay, self.defaults.backoff_cap_s))
                return
        base = self.defaults.backoff_base_s
        delay = min(self.defaults.backoff_cap_s, base ** attempt)
        time.sleep(delay * (0.5 + random.random() / 2))

    def get_json(self, url: str, **kwargs: Any) -> tuple[Any, FetchResult]:
        result = self.fetch(url, **kwargs)
        return result.json(), result
