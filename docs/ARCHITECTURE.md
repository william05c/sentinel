# Architecture

## The shape of the thing

    source --fetch--> raw bytes (SHA-256, immutable)
           --parse--> normalised frames
           --validate--> warehouse tables
           --select--> canonical metrics (as-of aware)
           --bind--> report templates
           --verify--> PASS or FAIL

Five properties are structural, not aspirational:

1. **The raw layer is immutable and content-addressed.** Every byte fetched is
   written once under its SHA-256. Re-parsing never needs the network, so a parser
   bug can be fixed and replayed over history without re-downloading anything.
2. **Revisions are kept, never overwritten.** A restatement adds rows to
   `fact_xbrl`. "Latest" is a query, not an UPDATE. This is what makes point-in-time
   research possible at all.
3. **Canonical metrics are selected at query time** from an ordered tag chain,
   against an as-of timestamp. The materialised `fact_xbrl_canonical` table is a
   cache for "now"; any other as-of rebuilds from the fact table.
4. **Reports bind numbers to queries.** A template cannot print a figure without a
   `(sql, params, formatter)` triple, which is written to `*.bindings.json` beside
   the report.
5. **The verifier is a build gate.** It rebuilds the report's context, re-executes
   every binding, and fails on mismatch, leftover token, or a number in the prose
   that no binding accounts for.

## Components

| Module | Responsibility |
|---|---|
| `crawler/net.py` | The only path to the network: per-host token bucket, retries with jitter, circuit breaker, content-addressed raw store, request log |
| `adapters/sec.py` | Pure parsers for submissions, company facts, company concepts |
| `warehouse/schema.sql` | Tables, provenance columns, revision semantics |
| `normalize/canonical.py` | Tag chains, period bucketing (52/53-week safe), as-of selection |
| `reports/render.py` | Binding, formatting, deterministic substitution |
| `reports/dossier.py` | The first report: company dossier |
| `verify.py` | The numeric gate |
| `ingest.py` | Orchestration plus `ingest_run` accounting |

## The as-of design

Every fact row carries `accepted_at`: the moment the SEC accepted the filing, joined
from the submissions index because the company-facts payload does not contain it.

`build_canonical(con, cik, as_of)` selects only facts with `accepted_at <= as_of`
and, within a period, prefers the most recent such fact and the highest-priority tag.
The consequence: a report generated with `as_of = 2020-01-01` cannot contain a 2021
restatement, and a test proves it.

Fiscal periods are bucketed by duration, not by calendar arithmetic:

    instant                 -> 'I'
    duration 350-380 days   -> 'FY'
    duration 80-100 days    -> 'Q'

A 52/53-week retailer's fiscal year is 364 or 371 days; it lands in 'FY' without
being misaligned to a calendar quarter, which is the failure mode that makes
retail figures silently wrong for a week every year.

## What we borrowed, what we rejected, and why

**SEC EDGAR APIs (data.sec.gov).** The backbone, and the reason this system does not
crawl HTML filings for financial data. Company facts gives a decade of tagged facts in
one request, which is orders of magnitude politer than walking filing pages. Taken
wholesale; the only design addition is joining `accepted_at` from submissions, because
the facts payload does not carry an acceptance timestamp and point-in-time needs one.

**SEC bulk data sets.** Taken as the backfill path, with a correction to the plan:
one quarter of financial-statement data is 124 MB compressed, so the naive "download
everything" approach would either exhaust memory or take days on a laptop. The adapter
records the size reality and streams from disk.

**nlpaueb/edgar-crawler.** Studied for its section taxonomy and its "extract sections,
not whole documents" discipline, which is the right instinct for 10-K text. Not copied:
it is GPL-3.0, and this repository is MIT. For v0.1 it is not a dependency at all,
because text extraction is Phase 3 work; when it lands, the design will be an original
implementation of the same idea with its own section map.

**dgunning/edgartools.** The best-in-class Python EDGAR object model, and the reference
for how to normalise a filing's financial statements into consistent line items. Rejected
as a dependency for v0.1 for a specific reason: it would put a second, opaque selection
layer between the raw facts and the canonical metrics, and the entire point of this
pipeline is that the selection is explicit and testable. Its API ergonomics informed
ours.

**jadchaar/sec-edgar-downloader.** Its batch/download ergonomics (company and form
filters, resumable batches, sane retries) are the model for our fetcher. Not vendored:
our fetcher needs per-host token buckets and a content-addressed store, which is a
different concern from a downloader's user-facing filters.

**OpenBB.** Borrowed the provider-adapter pattern: one declared metadata block per
source, uniform record shapes, explicit fallbacks. Rejected the breadth: 100+ providers
is a liability when the requirement is six SEC endpoints that must be *exactly* right.
The registry concept survived; the scale did not.

**microsoft/qlib.** Borrowed the point-in-time store discipline and the idea that factor
evaluation is a first-class artefact. Rejected its expression engine: for 30 metrics a
YAML tag chain is auditable by reading it, and an expression language is a second thing
that can be wrong.

**stefan-jansen/machine-learning-for-trading.** Borrowed the evaluation posture
(information coefficient, quantile spreads, decay) for Phase 4. Nothing taken yet;
recorded here so the later work has a named standard rather than an improvised one.

**virattt/ai-hedge-fund.** Studied as the counter-example it is: a multi-agent system
producing fluent prose with numbers that were never computed. Its report *structure*
(personas, sections) is useful; its failure mode is precisely what the numeric
verification gate exists to prevent. Rejected entirely as a design.

**sharadar SF1 (commercial).** Read as the reference schema for point-in-time
fundamentals: one row per (ticker, dimension, datekey), with as-reported versus
restated columns. Not a dependency, but its schema is why `fact_xbrl` keys on
(accession, tag, period) rather than (company, period).

**edgarWebR (R).** Borrowed the query shape for full-text hunting ("all filers with
this phrase in this form type in this window"), which maps directly onto the EFTS
endpoint. Not used at runtime.
