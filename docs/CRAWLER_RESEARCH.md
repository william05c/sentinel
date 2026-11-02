# Crawler research: what polite crawling actually requires

Research done before changing the fetcher, with every claim tied to a source and to a
line of this repository. The short version: the crawler was missing robots.txt entirely,
had no conditional requests, and had no ceiling on how much it could fetch in a day.

## Sources

| Source | What it settles |
|---|---|
| RFC 9309, Robots Exclusion Protocol | What robots.txt means, and what to do when it cannot be read |
| RFC 9111, HTTP Caching | Conditional requests, 304, revalidation |
| Scrapy AutoThrottle docs | Adaptive delay from observed load rather than a fixed rate |
| SEC Fair Access policy | 10 requests/second, declared User-Agent, bulk preferred |
| Cho & Garcia-Molina, *Effective Page Refresh Policies for Web Crawlers* | Revisit scheduling as a value/age trade-off |

## The rules worth quoting

**Unreachable robots means disallow everything.** RFC 9309 §2.3.1.4:

> If the robots.txt file is unreachable due to server or network errors, this means the
> robots.txt file is undefined and the crawler MUST assume complete disallow.

Almost every crawler I have read does the opposite — it treats a 5xx as "no rules" and
keeps going. That is the wrong way round, and it fails in exactly the situation where a
site is already struggling.

**4xx means no rules exist**, so access is permitted. **Parsing must be tolerant**: the
RFC requires parsing every line and using whatever is parseable, with a limit of at
least 500 KiB.

**Caching is allowed but bounded.** RFC 9309 §2.4:

> Crawlers SHOULD NOT use the cached version for more than 24 hours, unless the
> robots.txt file is unreachable.

**Matching is longest-match on the path**, with `*` as a wildcard and `$` anchoring the
end; an equal-length match goes to Allow. Most specific User-Agent group applies — the
wildcard group does not merge with a group that names you.

**crawl-delay is not in RFC 9309.** It is a de-facto extension. Honouring it is polite;
treating it as authoritative over your own rate limiter is not, because a site can
publish anything.

## What was wrong in this repository

| Requirement | Before | Now |
|---|---|---|
| robots.txt | **not read at all** | fetched once per host, 24 h cache, RFC status semantics |
| Conditional GET | none | ETag / If-Modified-Since, 304 served from cache |
| Daily ceiling | none | per-source requests and bytes, persisted across restarts |
| Adaptive delay | fixed rate | multiplies on 429/5xx, decays on success |
| Retry-After | honoured | honoured (unchanged) |
| Bulk over scraping | followed by policy | unchanged |

The missing robots check was the serious one. It is a compliance claim the README makes
("public data only; identify yourself; obey robots") that the code did not keep.

## Verified against the real host

SEC publishes robots.txt, and the paths this project uses are permitted:

    Allow: /Archives/edgar/data
    Disallow: /Archives/bin
    Disallow: /Archives/etc

`data.sec.gov` returns **404** for robots.txt, which is "unavailable" and therefore
permissive. A live check after the change:

    OK 200 797604 https://www.sec.gov/files/company_tickers.json
    OK 200 163586 https://data.sec.gov/submissions/CIK0000320193.json

## What is deliberately still missing

- **No frontier or URL prioritisation.** This crawler is driven by a known universe and a
  dated index, not by links, so a frontier queue would be machinery without a use.
- **No distributed politeness.** One process, one host budget. If the backfill ever runs
  in parallel, the limiter has to move to shared state first.
- **No crawl-trap detection.** A fixed universe cannot wander into one.
- **robots.txt is not cached across processes**, only in memory and on the raw store.
