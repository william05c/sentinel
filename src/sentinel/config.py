"""Configuration and path resolution.

Two rules live here:

1. Paths are derived from one root, so a report can record where its data came from.
2. The SEC requires a declared contact address in the User-Agent. It is read from
   the environment, never hardcoded, because this repository is public.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path

import yaml

CONTACT_ENV = "SENTINEL_CONTACT_EMAIL"
UA_ENV = "SENTINEL_USER_AGENT"
HOME_ENV = "SENTINEL_HOME"

DEFAULT_UA = "sentinel-research/0.1"


def repo_root() -> Path:
    """Locate the project root.

    Installed editable (the normal case), the package sits at <root>/src/sentinel,
    so the root is three levels up. If that is not a project root, fall back to the
    current directory, and failing that complain loudly rather than guessing.
    """
    env = os.environ.get(HOME_ENV)
    if env:
        return Path(env).expanduser().resolve()

    here = Path(__file__).resolve()
    for cand in (here.parents[2] if len(here.parents) > 2 else here.parent, Path.cwd()):
        if (cand / "pyproject.toml").exists():
            return cand
    raise RuntimeError(
        "Could not locate the SENTINEL project root. Set SENTINEL_HOME to the "
        "directory containing pyproject.toml."
    )


@dataclass(frozen=True)
class Paths:
    root: Path
    config: Path
    data: Path
    raw: Path
    warehouse_dir: Path
    db: Path
    reports: Path
    cache_index: Path
    crawl_log: Path
    snapshots: Path

    @classmethod
    def build(cls, root: Path | None = None) -> Paths:
        r = (root or repo_root()).resolve()
        data = r / "data"
        return cls(
            root=r,
            config=r / "config",
            data=data,
            raw=data / "raw",
            warehouse_dir=data / "warehouse",
            db=data / "warehouse" / "sentinel.duckdb",
            reports=r / "reports" / "out",
            cache_index=data / "raw" / "index.json",
            crawl_log=data / "crawl_log.jsonl",
            snapshots=data / "snapshots",
        )

    def ensure(self) -> None:
        for d in (self.data, self.raw, self.warehouse_dir, self.reports, self.snapshots):
            d.mkdir(parents=True, exist_ok=True)


@dataclass(frozen=True)
class Identity:
    """Who we say we are when knocking on someone's server."""

    user_agent: str
    contact: str

    @property
    def header(self) -> str:
        return f"{self.user_agent} ({self.contact})" if self.contact else self.user_agent


def identity() -> Identity:
    ua = os.environ.get(UA_ENV, DEFAULT_UA)
    contact = os.environ.get(CONTACT_ENV, "")
    return Identity(user_agent=ua, contact=contact)


@dataclass(frozen=True)
class SourceSpec:
    name: str
    hosts: tuple[str, ...]
    requests_per_second: float
    burst: int
    cache_ttl_s: int
    license: str
    enabled: bool
    description: str = ""


@dataclass(frozen=True)
class Defaults:
    max_retries: int
    timeout_s: float
    backoff_base_s: float
    backoff_cap_s: float


def _read_yaml(path: Path) -> dict:
    with path.open("r", encoding="utf-8") as fh:
        return yaml.safe_load(fh) or {}


@lru_cache(maxsize=1)
def sources_config() -> tuple[dict[str, SourceSpec], Defaults]:
    root = repo_root()
    doc = _read_yaml(root / "config" / "sources.yaml")
    defaults = doc.get("defaults", {})
    specs: dict[str, SourceSpec] = {}
    host_index: dict[str, str] = {}
    for name, raw in (doc.get("sources") or {}).items():
        spec = SourceSpec(
            name=name,
            hosts=tuple(raw.get("hosts", [])),
            requests_per_second=float(raw.get("requests_per_second", 1)),
            burst=int(raw.get("burst", 1)),
            cache_ttl_s=int(raw.get("cache_ttl_s", 0)),
            license=str(raw.get("license", "")),
            enabled=bool(raw.get("enabled", False)),
            description=str(raw.get("description", "")),
        )
        specs[name] = spec
        for h in spec.hosts:
            host_index[h.lower()] = name
    d = Defaults(
        max_retries=int(defaults.get("max_retries", 4)),
        timeout_s=float(defaults.get("timeout_s", 60)),
        backoff_base_s=float(defaults.get("backoff_base_s", 1.5)),
        backoff_cap_s=float(defaults.get("backoff_cap_s", 30)),
    )
    return {"specs": specs, "by_host": host_index}, d  # type: ignore[return-value]


def source_for_host(host: str) -> SourceSpec | None:
    cfg, _ = sources_config()
    name = cfg["by_host"].get(host.lower())  # type: ignore[index]
    return cfg["specs"].get(name) if name else None  # type: ignore[index]


@lru_cache(maxsize=1)
def metrics_config() -> dict:
    root = repo_root()
    return _read_yaml(root / "config" / "metrics.yaml")
