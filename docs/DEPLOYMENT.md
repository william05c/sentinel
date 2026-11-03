# Deployment

Three ways to run this, in order of how well they are tested.

## 1. Web (verified)

    sentinel serve --port 8788

Then open http://127.0.0.1:8788 — API docs at /docs, dashboard at /dashboard.html.

Verified running:

    GET /api/health          {"status":"ok","companies":707,"facts":4586395,"findings":2}
    GET /api/companies?q=apple
    GET /api/company/320193
    GET /api/digest

Endpoints: `/api/health`, `/api/companies`, `/api/company/{cik}`, `/api/findings`,
`/api/digest`, `POST /api/crawl`. The app binds to 127.0.0.1 by default; there is no
authentication and it is not built to face the internet.

## 2. Autonomous (script provided, not yet executed here)

    powershell -ExecutionPolicy Bypass -File scripts/schedule_watch.ps1

Registers a daily Windows Scheduled Task running `sentinel watch --days 2`. The watch is
already quiet by default and reports its own coverage, so an unattended run either says
"nothing material" or explains what changed.

A run is idempotent: re-scanning the same day adds no events.

## 3. Executable (script provided, build not executed here)

    powershell -ExecutionPolicy Bypass -File scripts/build_exe.ps1

Produces `dist\sentinel.exe` (expect 150–250 MB: duckdb, pandas and lxml are bundled).
The exe needs `config/` and a writable `data/` beside it. The browser extra is **not**
bundled — Playwright's browsers cannot live inside a single file.

## The browser backend (crawl4ai)

crawl4ai is integrated as an **optional rendering backend**, not as the crawler.

    pip install "sentinel[browser]"
    python -m playwright install chromium
    sentinel fetchmd --url https://example.com

It exists for pages that JavaScript builds at runtime. It is not used for SEC data,
which arrives as JSON and HTML that httpx reads directly — pulling Playwright, patchright
and litellm into the default install to fetch JSON faster would be a bad trade.

**It is still polite.** `browser.fetch_polite` checks robots.txt and the source budget
through the same fetcher before a browser is launched, and stores the rendered HTML in
the content-addressed raw store so rendered pages keep their provenance.

Status: the adapter is written against crawl4ai 0.9.5's published API
(`AsyncWebCrawler`, `BrowserConfig`, `CrawlerRunConfig`, `CacheMode`) and guarded by an
availability check. **It has not been executed here** — installing crawl4ai pulls several
hundred megabytes and was out of scope for this environment. Treat `sentinel fetchmd` as
unverified until you run it once.
