# Source Registry

Every source the system touches, what a live probe found on 2026-10-21, and the
constraints that follow. **The probe result is the authority.** Where the build
spec described something differently, the discrepancy is recorded in the last
section rather than quietly corrected in code.

Probe method: sequential requests, 1.1 s apart, declared User-Agent
`SENTINEL-research/0.1 (william.cossey05@yahoo.com)`, from a Windows workstation.

---

## 1. Tier 0 — required, free, no key

| # | Endpoint | Status | Latency | Size | Notes |
|---|---|---|---|---|---|
| 1 | `www.sec.gov/files/company_tickers.json` | 200 | 1038 ms | 781 KB | Ticker/CIK/exchange spine. Keyed by arbitrary integer, not ticker |
| 2 | `data.sec.gov/submissions/CIK##########.json` | 200 | 513 ms | 160 KB | Full filing history for AAPL, incl. former names |
| 3 | `data.sec.gov/api/xbrl/companyconcept/CIK##########/us-gaap/Assets.json` | 200 | 79 ms | 20 KB | One tag across time; used for reconciliation |
| 4 | `data.sec.gov/api/xbrl/frames/us-gaap/Assets/USD/CY2023Q1I.json` | 200 | 741 ms | 820 KB | Cross-sectional snapshot |
| 5 | `www.sec.gov/Archives/edgar/full-index/2024/QTR3/company.idx` | 200 | 3876 ms | **41 MB** | Uncompressed quarterly index |
| 6 | `www.sec.gov/Archives/edgar/daily-index/2024/QTR3/form.20240801.idx` | 200 | 340 ms | 868 KB | Daily index — the change feed |
| 7 | `efts.sec.gov/LATEST/search-index?q="going concern"&forms=10-K` | 200 | 884 ms | 57 KB | Full-text search |
| 8 | `www.sec.gov/files/dera/data/financial-statement-data-sets/2024q1.zip` | 200 | 553 ms | **124 MB** | Bulk numeric facts |
| 9 | `www.sec.gov/data-research/sec-markets-data/insider-transactions-data-sets` | 200 | 371 ms | 129 KB | Landing page; zip URLs must be scraped |
| 10 | `www.sec.gov/data-research/sec-markets-data/financial-statement-data-sets` | 200 | 688 ms | 119 KB | Landing page; zip URLs must be scraped |

### 1.1 Observed schemas

**submissions** (top-level keys, AAPL):

    cik, entityType, sic, sicDescription, ownerOrg,
    insiderTransactionForOwnerExists, insiderTransactionForIssuerExists,
    name, tickers, exchanges, formerNames, fiscalYearEnd,
    stateOfIncorporation, addresses, filings{recent{...,files[]}}

`filings.recent` is **column-oriented**: parallel arrays for accessionNumber,
filingDate, reportDate, acceptanceDateTime, form, items, primaryDocument,
primaryDocDescription, isXBRL, size. Parsing it as a list of records is the first
mistake a naive adapter makes.

**companyconcept**: `cik, taxonomy, tag, label, description, entityName, units{unit -> [facts]}`.
Each fact carries `start` (absent for instants), `end`, `val`, `accn`, `fy`, `fp`,
`form`, `filed`, `frame`. **There is no acceptance timestamp here** — it must be joined
from submissions, which is why filing metadata is ingested first.

**frames**: `taxonomy, tag, ccp, uom, label, description, pts, data[]`.

**EFTS search**: `took, timed_out, _shards, hits, aggregations, query`.

---

## 2. Constraints that follow from the probe

1. **The bulk datasets are discovered, not hardcoded.** The insider-transaction zips
   moved from `/files/structureddata/...` (2025q4) to `/files/datastandardsinnovation/...`
   (2026q3) between quarters. A hardcoded base path breaks silently on the next refresh.
   The adapter parses the landing page and takes the current links.
2. **A 403 from `sec.gov` does not mean rate limited.** A wrong archive path returns
   403, not 404 (observed on `.../daily-index/2024/QTR3/2024/08/2024-08-01/form.idx`).
   Retrying is correct; treating 403 as "empty result" is how a pipeline grows holes.
3. **Bulk is not cheap by default.** One quarter of financial-statement data is 124 MB
   compressed; four quarters is ~0.5 GB per year of coverage. The 15-year backfill cannot
   naively unzip 60 quarters into memory.
4. **The daily index is the change feed,** at 868 KB/day. It is what makes
   "fetch 500 companies to find the 12 that filed" unnecessary.
5. **`company.idx` is 41 MB uncompressed** and must never be loaded whole into a
   dataframe; stream it line by line.

---

## 3. Tier 1 — free tier with key (optional, not enabled)

Alpha Vantage, Financial Modeling Prep, Finnhub, Tiingo, Polygon. None are probed here
because no keys are configured, and none are required for the vertical slice. Per the
spec, their documented limits are to be read from provider docs at build time rather
than hardcoded from memory.

## 4. Tier 2 — optional

GDELT, USPTO PatentsView, BLS, BEA, Nasdaq short interest. Not implemented. No claims
are made about them.

---

## 5. Discrepancies against the build specification

| Spec claim | Reality | Consequence |
|---|---|---|
| "SEC bulk data sets ... hundreds of thousands of facts in one download" | True, but 124 MB per quarter compressed | Backfill needs streaming unzip and per-quarter checkpoints |
| Implied stable bulk URLs | Path base changed across quarters | Discovery step required; recorded here |
| "company facts ... single-digit to tens of MB" | AAPL facts are large; the parser filters to dictionary tags | Documented scope decision in LIMITATIONS.md |
| 403 implies access problem | 403 also means "no such path" | Retry then surface the URL; never treat as empty |
| Daily index path not specified | `form.YYYYMMDD.idx`, not `/YYYY/MM/DD/form.idx` | Adapter constructs the observed form |

---

## 6. Rate limits in force

| Source | Documented | Configured | Rationale |
|---|---|---|---|
| www.sec.gov | 10 req/s | 8 req/s, burst 3 | Stay under the published ceiling by design |
| data.sec.gov | 10 req/s | 8 req/s, burst 3 | Same |
| efts.sec.gov | not stated | 5 req/s | Conservative; search is expensive server-side |

Requests are serialised per host with a minimum inter-request interval. There is no
parallelism across hosts in v0.1 — the 500-company backfill will revisit this.
