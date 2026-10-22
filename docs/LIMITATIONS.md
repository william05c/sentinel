# Limitations

An honest list of what this system cannot do yet, and why. Anything here that looks
like a gap is a gap; nothing is silently approximated.

## Scope

- **One company per ingest.** There is no universe builder yet: you supply a ticker or
  CIK. S&P 500 and Nasdaq membership history is Phase 2.
- **No delisted companies.** The schema supports status and successor links; nothing
  populates them yet. Until Phase 2, any claim about survivorship is untested.
- **One report.** The company dossier exists. Peer comparison, screening, correlation,
  importance, event studies, ownership, forecasting, digest and capital allocation are
  specified and not built.

## Data

- **Facts are filtered to the metric dictionary.** Only tags in `config/metrics.yaml`
  are stored. A tag outside the dictionary is invisible to every downstream query. This
  keeps the warehouse small and the selection auditable, at the cost of completeness.
- **Filing metadata is capped at 1000 recent filings** by the submissions payload. Older
  filings require following `filings.files`, which is not implemented.
- **No prices, ownership, 13F, text, or macro data yet.** The tables exist and are empty.
  Any report that implies otherwise is wrong.
- **Foreign private issuers (20-F, IFRS)** are not handled. Ingesting one today would
  silently produce an empty dossier rather than an error, which is a defect to fix in
  Phase 3.
- **Fiscal year identification uses the XBRL `fy` field and period duration**, not the
  company's stated fiscal calendar. This is right for 52/53-week filers and wrong for a
  company that changes its year-end mid-history.

## Statistics

- **No analytic has run on real data yet.** Correlation, importance and forecast code
  does not exist. The bias register is specified; the tests that prove the controls work
  do not exist.
- **Reconciliation covers three tags** (Assets, Revenues, NetIncomeLoss) on one company.
  90 comparisons matched exactly; that is evidence for this company, not a guarantee
  across the universe.

## Operations

- **No scheduler.** The watch is Phase 6. Nothing runs unattended.
- **No dashboard.** Phase 7.
- **Windows only, in practice.** The CI matrix includes Ubuntu, but the bootstrap script
  and every command in the docs are PowerShell.
- **Email delivery is unimplemented.** The design and the credential rules are written
  down; there is no notifier.

## Known defects to carry forward

- The verifier allows extra occurrences of a bound value (a count of "101" can coincide
  with digits elsewhere). It catches missing occurrences, not unexpected ones.
- `ingest_run.status` records "error" but the exception is re-raised before the row is
  committed in some paths, so a hard crash can leave no run record.

## Analytics (updated after Phase 4)

- **Peer matching does not work.** Precision@10 is 0 of 20. The fingerprint is fourteen
  financial ratios; that is not enough to identify a business. Do not use `sentinel peers`
  for anything.
- **No prices, therefore no returns.** Stooq serves a JavaScript challenge and Yahoo
  returns 429 from this machine. Correlation, importance, event studies and forecasting
  are blocked on a price source, not on code.
- **Beneish M and Ohlson O are not implemented.** Piotroski F, Altman Z/Z'' and Sloan
  accruals are.
- **Altman currently returns n/a** until one reparse populates current assets and
  current liabilities; the score is withheld rather than approximated.
