"""Optional browser backend: crawl4ai, used only where HTTP cannot reach.

crawl4ai renders pages with a real browser and returns markdown. That is the right tool
for a JavaScript-driven page and the wrong tool for the SEC, whose data arrives as JSON
and HTML that httpx reads in milliseconds. Pulling Playwright, patchright and litellm
into the default install to fetch JSON faster is not a trade worth making, so this is an
opt-in extra:

    pip install "sentinel[browser]"
    python -m playwright install chromium

Everything here degrades to a clear ImportError rather than an obscure failure, and the
politeness rules still apply: the caller must clear robots and budget first, because a
browser is not an excuse to ignore either.
"""

from __future__ import annotations

import importlib.util
from dataclasses import dataclass
from typing import Any

INSTALL_HINT = (
    'crawl4ai is not installed. Install the browser extra and its browser binaries: '
    'pip install "sentinel[browser]" && python -m playwright install chromium'
)


def available() -> bool:
    return importlib.util.find_spec("crawl4ai") is not None


@dataclass
class Rendered:
    url: str
    status: int
    html: str
    markdown: str

    @property
    def ok(self) -> bool:
        return 200 <= self.status < 400


async def render(url: str, *, timeout_ms: int = 45000, headless: bool = True) -> Rendered:
    """Render one URL with crawl4ai and return HTML plus markdown."""
    if not available():
        raise ImportError(INSTALL_HINT)

    from crawl4ai import AsyncWebCrawler, BrowserConfig, CacheMode, CrawlerRunConfig

    browser_config = BrowserConfig(headless=headless, verbose=False)
    run_config = CrawlerRunConfig(cache_mode=CacheMode.BYPASS, page_timeout=timeout_ms)
    async with AsyncWebCrawler(config=browser_config) as crawler:
        result: Any = await crawler.arun(url=url, config=run_config)
    markdown = ""
    try:
        markdown = result.markdown.raw_markdown or ""
    except Exception:
        markdown = str(getattr(result, "markdown", "") or "")
    return Rendered(url=url, status=int(getattr(result, "status_code", 0) or 0),
                    html=str(getattr(result, "html", "") or ""), markdown=markdown)


def render_sync(url: str, **kwargs) -> Rendered:
    """Blocking wrapper for CLI use."""
    import asyncio
    return asyncio.run(render(url, **kwargs))


def fetch_polite(fetcher, url: str) -> Rendered:
    """Render a URL through crawl4ai only after the polite path has cleared it.

    The fetcher owns robots, the rate limiter and the daily budget. Skipping them because
    a request goes through a browser would defeat the point of having them.
    """
    from urllib.parse import urlparse

    from sentinel.crawler import robots as robots_mod
    from sentinel.crawler.net import SourceError

    parsed = urlparse(url)
    policy = fetcher._robots_for(parsed.netloc)
    if not policy.allowed(parsed.path or "/"):
        raise SourceError(url, None, "disallowed by robots.txt")
    rendered = render_sync(url)
    # Store the rendered HTML so the page has provenance like anything else we fetch.
    if rendered.html:
        fetcher.store.write(url, rendered.html.encode("utf-8"), rendered.status, "text/html; rendered")
    return rendered
