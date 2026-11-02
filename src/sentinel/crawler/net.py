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
from sentinel.crawler import robots as robots_mod

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

    def acquire(self, host: str, spec: SourceSpec | None, factor: float = 1.0) -> None:
        state = self._hosts.setdefault(host, _HostState())
        now = self._clock()
        if state.open_until > now:
            raise SourceError(f"https://{host}/", None,
                              f"circuit open for {state.open_until - now:.1f}s")
        if spec is None or spec.requests_per_second <= 0:
            interval = 1.0
        else:
            interval = (1.0 / spec.requests_per_second) * factor
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


class Budget:
    """A per-day ceiling on requests and bytes, per source.

    A crawler with no ceiling is one bug away from a denial of service it authored. The
    counter is persisted so a restart does not silently reset the allowance.
    """

    def __init__(self, path: Path):
        self.path = path
        self._state = self._load()

    def _today(self) -> str:
        return utcnow().date().isoformat()

    def _load(self) -> dict:
        try:
            state = json.loads(self.path.read_text(encoding="utf-8"))
            if state.get("date") == self._today():
                return state
        except Exception:
            pass
        return {"date": self._today(), "requests": {}, "bytes": {}}

    def _save(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_suffix(".tmp")
        tmp.write_text(json.dumps(self._state), encoding="utf-8")
        tmp.replace(self.path)

    def check(self, source: str, spec: SourceSpec | None) -> None:
        if spec is None:
            return
        used_r = self._state["requests"].get(source, 0)
        used_b = self._state["bytes"].get(source, 0)
        if used_r >= spec.max_requests_per_day:
            raise SourceError("budget", None, f"daily request budget exhausted for {source} ({used_r})")
        if used_b >= spec.max_bytes_per_day:
            raise SourceError("budget", None, f"daily byte budget exhausted for {source} ({used_b})")

    def record(self, source: str, size: int) -> None:
        self._state["requests"][source] = self._state["requests"].get(source, 0) + 1
        self._state["bytes"][source] = self._state["bytes"].get(source, 0) + size
        self._save()


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

    def write(self, url: str, content: bytes, status: int, content_type: str,
              etag: str | None = None, last_modified: str | None = None) -> str:
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
            # kept for conditional requests: a 304 costs one round trip, not a download
            "etag": etag,
            "last_modified": last_modified,
        }
        self.flush()
        return sha

    def touch(self, url: str, etag: str | None = None, last_modified: str | None = None) -> None:
        entry = self._index.get(url)
        if not entry:
            return
        entry["fetched_at"] = utcnow().isoformat()
        if etag:
            entry["etag"] = etag
        if last_modified:
            entry["last_modified"] = last_modified
        self.flush()

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
        self._robots: dict[str, robots_mod.RobotsPolicy] = {}
        self._delay_factor: dict[str, float] = {}
        self.budget = Budget(paths.data / "crawl_budget.json")
        self._logger = logger or self._default_log

    def _robots_for(self, host: str) -> robots_mod.RobotsPolicy:
        """Cached per host for at most 24 hours, per RFC 9309 2.4."""
        cached = self._robots.get(host)
        if cached is not None and not cached.stale():
            return cached
        product = self.identity.user_agent.split("/")[0]
        try:
            response = self._client.get("https://" + host + "/robots.txt")
            policy = robots_mod.for_status(response.status_code, response.text, product)
        except httpx.HTTPError:
            # unreachable means undefined, which means assume complete disallow
            policy = robots_mod.for_status(590, None, product)
        self._robots[host] = policy
        return policy

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
            return self._from_cache(url)

        # Robots first: a disallowed path is not fetched at all, and the check is cached
        # per host so it costs one request per day.
        is_robots = parsed.path == "/robots.txt"
        if not is_robots:
            policy = self._robots_for(host)
            if not policy.allowed(parsed.path or "/"):
                raise SourceError(url, None, "disallowed by robots.txt")
            if policy.crawl_delay and spec is not None:
                ttl = ttl  # crawl-delay is applied through the limiter factor below

        if spec is not None:
            self.budget.check(spec.name, spec)

        entry = self.store.lookup(url)
        conditional = {}
        if allow_cache and entry:
            if entry.get("etag"):
                conditional["If-None-Match"] = entry["etag"]
            if entry.get("last_modified"):
                conditional["If-Modified-Since"] = entry["last_modified"]

        factor = self._delay_factor.get(host, 1.0)
        if not is_robots:
            policy = self._robots.get(host)
            if policy is not None and policy.crawl_delay:
                configured = 1.0 / (spec.requests_per_second if spec else 1.0)
                factor = max(factor, policy.crawl_delay / max(configured, 1e-6))

        last_error: Exception | None = None
        for attempt in range(1, self.defaults.max_retries + 1):
            self.limiter.acquire(host, spec, factor)
            started = time.perf_counter()
            try:
                response = self._client.get(url, headers=conditional)
            except httpx.HTTPError as exc:  # network-level failure
                last_error = exc
                self.limiter.note_failure(host)
                self._sleep_backoff(attempt)
                continue

            elapsed_ms = int((time.perf_counter() - started) * 1000)
            self._logger({"ts": utcnow().isoformat(), "url": url, "status": response.status_code,
                          "bytes": len(response.content), "ms": elapsed_ms, "attempt": attempt,
                          "cache_hit": False, "user_agent": self.identity.header})
            if spec is not None:
                self.budget.record(spec.name, len(response.content))

            # 304 means our copy is still current: refresh its timestamp, do not re-read.
            if response.status_code == 304 and entry:
                self.store.touch(url, entry.get("etag"), entry.get("last_modified"))
                self._ease(host)
                return self._from_cache(url)

            if response.status_code < 400:
                sha = self.store.write(url, response.content, response.status_code,
                                       response.headers.get("content-type", ""),
                                       etag=response.headers.get("ETag"),
                                       last_modified=response.headers.get("Last-Modified"))
                self.limiter.note_success(host)
                self._ease(host)
                return FetchResult(url, response.status_code, response.content, sha, utcnow(),
                                   False, response.headers.get("content-type", ""), elapsed_ms)

            if response.status_code not in RETRY_STATUSES:
                raise SourceError(url, response.status_code, "non-retryable HTTP status")

            last_error = SourceError(url, response.status_code, "retryable HTTP status")
            self.limiter.note_failure(host)
            self._tighten(host)
            self._sleep_backoff(attempt, retry_after=response.headers.get("Retry-After"))

        raise SourceError(url, None, f"exhausted {self.defaults.max_retries} attempts: {last_error}")

    def _from_cache(self, url: str) -> FetchResult:
        entry = self.store.lookup(url)
        assert entry is not None
        content = self.store.read(entry["sha256"])
        self._logger({"ts": utcnow().isoformat(), "url": url, "status": entry.get("status", 200),
                      "bytes": len(content), "ms": 0, "attempt": 0, "cache_hit": True,
                      "user_agent": self.identity.header})
        return FetchResult(url, int(entry.get("status", 200)), content, entry["sha256"],
                           datetime.fromisoformat(entry["fetched_at"]), True,
                           entry.get("content_type", ""), 0)

    def _tighten(self, host: str) -> None:
        """A refusal is information: slow down until the host stops objecting."""
        self._delay_factor[host] = min(8.0, self._delay_factor.get(host, 1.0) * 1.6)

    def _ease(self, host: str) -> None:
        self._delay_factor[host] = max(1.0, self._delay_factor.get(host, 1.0) * 0.85)

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
