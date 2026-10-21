# Build Log

A phase-by-phase record of how this was built, what the gate proved, and what was
deferred. Reading only this file should be enough to reconstruct the build.

Environment: Windows 11, Python 3.11.2, DuckDB 2.0.0, pandas 3.0.6, pytest 9.1.1.

---

## Phase 0 — Reconnaissance (2026-10-21)

**Commit:** `ca24dec` — docs: record live probe results for every Tier 0 source

**Gate:** nine endpoints probed sequentially, 1.1 s apart, with a declared
User-Agent. All returned data. Results and observed schemas are in
[docs/SOURCE_REGISTRY.md](SOURCE_REGISTRY.md).

| Endpoint | Status | Size |
|---|---|---|
| company_tickers.json | 200 | 781 KB |
| submissions (AAPL) | 200 | 160 KB |
| companyconcept Assets | 200 | 20 KB |
| frames CY2023Q1I | 200 | 820 KB |
| full-index company.idx | 200 | 41 MB |
| daily-index form.20240801.idx | 200 | 868 KB |
| EFTS full-text search | 200 | 57 KB |
| bulk financial statements 2024q1 | 200 | 124 MB |
| bulk insider transactions | 200 | 129 KB |

**What changed because reality disagreed with the plan**

1. Bulk-dataset URLs move between quarters (`/files/structureddata/...` →
   `/files/datastandardsinnovation/...`). Discovery from the landing page is now
   mandatory; a hardcoded base path would break silently.
2. A 403 from `sec.gov` can mean "no such path", not only "slow down". The retry
   policy treats both, and surfaces the URL.
3. One quarter of bulk financial data is 124 MB compressed, so the Phase 2 backfill
   must stream and checkpoint rather than load into memory.
4. `companyfacts` has no acceptance timestamp. `accepted_at` is joined from
   submissions, which is what makes the as-of contract possible at all.

**Deferred:** Tier 1/2 sources untouched (no keys configured, not required).

---

## Phase 1 — Vertical slice (2026-10-21)

**Commits:** `a562829` warehouse · `1d74830` crawler · `ee03887` ingest ·
`70b583f` reports · `875005d` tests

**Gate evidence (commands run, output quoted)**

    > sentinel ingest --ticker AAPL
    filings 1001 | xbrl 6600 | canonical 2561

    > sentinel report dossier --ticker AAPL
    snapshot 41a7220d8aaba7c2

    > sentinel verify reports\out\2026-10-21\2026-10-21_dossier_AAPL.md
    verification: PASS
      bindings checked: 71 (71 ok)

    > sentinel report dossier --ticker AAPL --as-of 2020-01-01
    > sentinel verify reports\out\2020-01-01\2020-01-01_dossier_AAPL.md
    verification: PASS
      bindings checked: 71 (71 ok)

    > pytest -q
    12 passed in 1.62s

**Point-in-time proof, by observation rather than assertion.** The 2020 view reports
FY2019 revenue of $260.2B and FY2019 net income of $55.3B. The current view reports
FY2025 revenue of $416.2B. The 2020 document contains no fact accepted after
2020-01-01, and no fiscal year later than 2019.

**Reconciliation.** 90 canonical values (Assets, Revenues, NetIncomeLoss) matched
against SEC's independently-served concept endpoint, exact to the dollar.

**Three defects the tests caught, which is the point of building them first**

1. The token regex was `[a-z0-9_]+`, so a form key like `form_10-K` was never
   substituted and left NUL bytes in the report. Caught by the file being unreadable.
2. The verifier's value-stripping used `str.replace`, so a short value such as "722"
   was removed from the middle of a snapshot hash, producing phantom unbound numbers.
   Fixed with boundary-aware matching.
3. Verification checked that a value *appeared*, not that it appeared as many times
   as it was used. A tampered summary figure hid behind an intact table figure. Fixed
   by counting uses at render time and failing when a use goes missing.

**Deviations from the specification**

- **Dependency installation** used the system Python's pip rather than `uv`. `uv`
  stalled repeatedly on large wheel downloads through this machine's proxy; pip with a
  regional mirror completed in about 90 seconds. The stack is unchanged.
- **Metric dictionary** currently defines 30 metrics / 57 tag mappings, not the 60
  required by the specification. This is Phase 3 scope and is listed as such; the
  schema and the loader already support 60+.
- **Dossier only.** One of the thirteen report types exists. The other twelve are
  Phase 7.

**Deferred to later phases, explicitly**

| Item | Phase |
|---|---|
| Universe backfill, bulk datasets, delisted names | 2 |
| 60+ metric dictionary, derived features, restatements surfaced in UI | 3 |
| Text extraction, Loughran-McDonald, narrative drift | 3 |
| Correlations, importance ranking, event studies, ownership flows | 4 |
| Forecast and calibration | 5 |
| The watch: change detection, digests, notifiers | 6 |
| Twelve remaining reports, dashboard, downloads | 7 |
| Remaining docs, runbook, bootstrap, performance measurement | 8 |

**Known gaps accepted for this phase**

- Filing metadata is capped at the 1000 most recent filings in the submissions
  payload; older filings need the paginated `filings.files` list.
- Ingestion filters company facts to dictionary tags, so a tag outside
  `config/metrics.yaml` is not stored. Documented in LIMITATIONS.md.
- No Parquet cold store yet (`pyarrow` is declared but unused).
