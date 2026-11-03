"""Firecrawl client: an optional hosted rendering and extraction service.

Firecrawl (v2.11.0) is a service, not a library you vendor. Self-hosting means a Node
stack plus Redis and a browser pool; using the hosted API means one HTTP call. This
module is the thin client for the hosted API, and it accepts a base URL so a
self-hosted instance works the same way.

    export FIRECRAWL_API_KEY=fc-...
    sentinel firecrawl --url https://example.com

Nothing in the SEC pipeline depends on it, for the same reason crawl4ai is optional:
the SEC serves JSON, not JavaScript.
"""

from __future__ import annotations

import os

DEFAULT_BASE = "https://api.firecrawl.dev"
INSTALL_HINT = "set FIRECRAWL_API_KEY (or FIRECRAWL_API_URL for a self-hosted instance)"


def available() -> bool:
    return bool(os.environ.get("FIRECRAWL_API_KEY") or os.environ.get("FIRECRAWL_API_URL"))


def base_url() -> str:
    return os.environ.get("FIRECRAWL_API_URL", DEFAULT_BASE).rstrip("/")


def scrape(url: str, *, formats: tuple[str, ...] = ("markdown",), timeout_s: float = 90.0) -> dict:
    """Scrape one URL. Returns the raw payload plus a normalised markdown field."""
    import httpx

    key = os.environ.get("FIRECRAWL_API_KEY", "")
    headers = {"Content-Type": "application/json"}
    if key:
        headers["Authorization"] = "Bearer " + key
    payload = {"url": url, "formats": list(formats)}
    with httpx.Client(timeout=timeout_s) as client:
        response = client.post(base_url() + "/v2/scrape", json=payload, headers=headers)
    if response.status_code >= 400:
        raise RuntimeError(f"firecrawl {response.status_code}: {response.text[:200]}")
    body = response.json()
    data = body.get("data", body) if isinstance(body, dict) else {}
    return {
        "url": data.get("metadata", {}).get("sourceURL", url),
        "markdown": data.get("markdown", "") or "",
        "status": int(data.get("metadata", {}).get("statusCode", 200) or 200),
        "raw": body,
    }
