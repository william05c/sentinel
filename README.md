# SENTINEL

A point-in-time research engine for US-listed companies. It crawls public SEC data,
stores every fact with its provenance, and writes reports whose numbers can be
re-derived from the database and checked by a machine.

The product is not a dashboard and not a price predictor. It is **findings with
provenance**: every number traceable to an accession number, every claim re-runnable.

## Status

| Phase | State |
|---|---|
| 0 — Reconnaissance | done — see [docs/SOURCE_REGISTRY.md](docs/SOURCE_REGISTRY.md) |
| 1 — Vertical slice | in progress |
| 2 — Universe backfill | not started |
| 3 — Dictionary and features | not started |
| 4 — Analytics | not started |
| 5 — Forecast | not started |
| 6 — The watch | not started |
| 7 — Reports and dashboard | not started |
| 8 — Hardening | not started |

## Quickstart (Windows)

    py -3.11 -m venv .venv
    .\.venv\Scripts\python.exe -m pip install -e .
    Copy-Item .env.example .env
    .\.venv\Scripts\sentinel.exe init
    .\.venv\Scripts\sentinel.exe ingest --ticker AAPL
    .\.venv\Scripts\sentinel.exe report dossier --ticker AAPL
    .\.venv\Scripts\sentinel.exe verify reports\out\<date>\<date>_dossier_AAPL.md

The SEC requires a declared contact address in the User-Agent. Put yours in
`SENTINEL_CONTACT_EMAIL` before the first fetch.

## What it can and cannot do today

**Can:** ingest one company's filing index and XBRL facts; select canonical metrics
through an ordered tag chain; produce a dossier whose every figure is bound to a query;
verify that report and fail when a number does not reproduce.

**Cannot yet:** build a universe (one company at a time, by CIK); read filing text;
compute correlations or forecasts; serve a dashboard; run unattended.

## Design commitments

- **Point-in-time or nothing.** Facts are never overwritten; every query is as-of.
- **No survivorship.** Delisted companies are retained once the universe exists.
- **No invented numbers.** Reports bind to SQL; the verifier fails on anything else.
- **Polite by construction.** Per-host rate limits, declared User-Agent, content-addressed cache.

## Data and licensing

Code is MIT. Data is not redistributed. SEC EDGAR data is public domain under the SEC's
fair-access policy; FRED, Ken French and index providers have their own terms, recorded
in [docs/SOURCE_REGISTRY.md](docs/SOURCE_REGISTRY.md).

## Disclaimer

Research output generated from public data. Not investment advice. Numbers are as of the
stated retrieval date and may since have been restated.
