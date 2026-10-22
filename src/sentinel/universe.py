"""Universe construction: index membership history and entity resolution.

Two things this module refuses to do, because they are how survivorship bias gets in:

1. Treat today's index as if it were always the index. Membership is stored as
   intervals with valid_from/valid_to, so "the S&P 500 in 2015" is a query, not an
   assumption.
2. Drop companies that left. 759 of the 1,262 ticker intervals on record have an end
   date. They stay in the warehouse, flagged, and can be ingested like any other name.

The membership dataset is fja05680/sp500 (MIT), which publishes ticker start and end
dates back to 1996. It is a curated dataset, not an official S&P publication; that is
recorded in the registry and in LIMITATIONS.
"""

from __future__ import annotations

import io
from datetime import date, datetime, timezone

import pandas as pd

from sentinel import SCHEMA_VERSION
from sentinel.warehouse import db

MEMBERSHIP_URL = (
    "https://raw.githubusercontent.com/fja05680/sp500/master/sp500_ticker_start_end.csv"
)
CURRENT_URL = "https://raw.githubusercontent.com/fja05680/sp500/master/sp500.csv"
SEC_TICKERS_URL = "https://www.sec.gov/files/company_tickers_exchange.json"

INDEX_CODE = "SP500"
SOURCE = "fja05680/sp500"


def parse_membership(text: str) -> pd.DataFrame:
    """ticker, start_date, end_date -> one row per membership interval."""
    frame = pd.read_csv(io.StringIO(text))
    frame.columns = [c.strip().lower() for c in frame.columns]
    frame["ticker"] = frame["ticker"].astype(str).str.strip()
    frame["valid_from"] = pd.to_datetime(frame["start_date"], errors="coerce").dt.date
    frame["valid_to"] = pd.to_datetime(frame["end_date"], errors="coerce").dt.date
    frame = frame[frame["ticker"].str.len() > 0]
    return frame[["ticker", "valid_from", "valid_to"]].reset_index(drop=True)


def parse_current(text: str) -> pd.DataFrame:
    frame = pd.read_csv(io.StringIO(text))
    frame.columns = [c.strip() for c in frame.columns]
    out = pd.DataFrame({
        "ticker": frame["Symbol"].astype(str).str.strip(),
        "name": frame.get("Security"),
        "sector": frame.get("GICS Sector"),
        "sub_industry": frame.get("GICS Sub-Industry"),
    })
    return out.reset_index(drop=True)


def store_membership(con, membership: pd.DataFrame, *, retrieved_at: datetime) -> int:
    """Replace the index membership table for this index. Idempotent."""
    db.run(con, "DELETE FROM dim_index_membership WHERE index_code = ?", [INDEX_CODE])
    frame = membership.copy()
    frame["index_code"] = INDEX_CODE
    frame["source"] = SOURCE
    frame["retrieved_at"] = retrieved_at.replace(tzinfo=None)
    frame["schema_version"] = SCHEMA_VERSION
    frame["cik"] = None
    con.register("_membership", frame)
    try:
        cols = ", ".join(frame.columns)
        db.run(con, "INSERT INTO dim_index_membership (" + cols + ") SELECT " + cols + " FROM _membership")
    finally:
        con.unregister("_membership")
    return len(frame)


def resolve_entities(con, exchange_tickers: dict, *, retrieved_at: datetime) -> dict:
    """Attach CIKs to membership intervals and seed dim_security.

    Resolution is deliberately allowed to fail: a ticker that no longer exists has no
    CIK in SEC's current file, and inventing one would be worse than a NULL. Those rows
    stay unresolved and are counted, so the gap is visible rather than silent.
    """
    rows = exchange_tickers.get("data") or []
    fields = exchange_tickers.get("fields") or []
    idx = {name: i for i, name in enumerate(fields)}
    by_ticker: dict[str, tuple[int, str, str]] = {}
    for row in rows:
        ticker = str(row[idx["ticker"]]).upper().strip()
        by_ticker[ticker] = (int(row[idx["cik"]]), str(row[idx["name"]]), str(row[idx["exchange"]]))

    con.execute("CREATE OR REPLACE TEMP TABLE _ticker_map(ticker VARCHAR, cik BIGINT, name VARCHAR, exchange VARCHAR)")
    mapping = pd.DataFrame(
        [{"ticker": t, "cik": c, "name": n, "exchange": e} for t, (c, n, e) in by_ticker.items()]
    )
    con.register("_map", mapping)
    try:
        db.run(con, "INSERT INTO _ticker_map SELECT ticker, cik, name, exchange FROM _map")
    finally:
        con.unregister("_map")

    db.run(con, """
        UPDATE dim_index_membership AS m
        SET cik = t.cik
        FROM _ticker_map AS t
        WHERE upper(m.ticker) = t.ticker AND m.index_code = ?
    """, [INDEX_CODE])

    resolved = db.scalar(con, "SELECT COUNT(*) FROM dim_index_membership WHERE index_code = ? AND cik IS NOT NULL", [INDEX_CODE])
    total = db.scalar(con, "SELECT COUNT(*) FROM dim_index_membership WHERE index_code = ?", [INDEX_CODE])

    securities = db.query(con, """
        SELECT DISTINCT m.cik, upper(m.ticker) AS ticker, t.exchange, m.valid_from, m.valid_to
        FROM dim_index_membership m
        JOIN _ticker_map t ON upper(m.ticker) = t.ticker
        WHERE m.index_code = ?
    """, [INDEX_CODE])
    if not securities.empty:
        securities["share_class"] = None
        securities["is_primary"] = True
        securities["source_system"] = "sec_edgar"
        securities["retrieved_at"] = retrieved_at.replace(tzinfo=None)
        securities["schema_version"] = SCHEMA_VERSION
        db.run(con, "DELETE FROM dim_security WHERE source_system = 'sec_edgar'")
        con.register("_sec", securities)
        try:
            cols = ", ".join(securities.columns)
            db.run(con, "INSERT INTO dim_security (" + cols + ") SELECT " + cols + " FROM _sec")
        finally:
            con.unregister("_sec")

    db.run(con, "DROP TABLE IF EXISTS _ticker_map")
    return {
        "membership_rows": int(total or 0),
        "resolved": int(resolved or 0),
        "unresolved": int((total or 0) - (resolved or 0)),
        "securities": len(securities),
        "sec_tickers": len(by_ticker),
    }


def build(con, fetcher) -> dict:
    """Fetch membership and current constituents, store them, resolve entities."""
    membership_result = fetcher.fetch(MEMBERSHIP_URL)
    membership = parse_membership(membership_result.text)
    rows = store_membership(con, membership, retrieved_at=membership_result.fetched_at)

    current_result = fetcher.fetch(CURRENT_URL)
    current = parse_current(current_result.text)
    current.to_parquet  # noqa: B018 - deliberately not stored as parquet
    con.register("_current", current)
    try:
        db.run(con, "CREATE OR REPLACE TEMP TABLE _current_tickers AS SELECT ticker FROM _current")
    finally:
        con.unregister("_current")

    tickers_result = fetcher.fetch(SEC_TICKERS_URL)
    resolution = resolve_entities(con, tickers_result.json(), retrieved_at=tickers_result.fetched_at)

    closed = db.scalar(
        con, "SELECT COUNT(*) FROM dim_index_membership WHERE index_code = ? AND valid_to IS NOT NULL",
        [INDEX_CODE],
    )
    return {
        "membership_rows": rows,
        "intervals_closed": int(closed or 0),
        "current_constituents": len(current),
        **resolution,
    }


def candidate_tickers(con, *, include_closed: bool = True, limit: int | None = None) -> pd.DataFrame:
    """Tickers to ingest, resolved ones only, ordered to be deterministic."""
    where = ["index_code = ?", "cik IS NOT NULL"]
    if not include_closed:
        where.append("valid_to IS NULL")
    # Closed intervals first. Ingesting the names that LEFT the index before the ones
    # that are still in it is deliberate: if the pipeline cannot handle a dead company
    # it should fail on the first batch, not after 500 live ones.
    sql = ("SELECT DISTINCT ticker, cik FROM dim_index_membership WHERE " + " AND ".join(where)
           + " ORDER BY valid_to NULLS LAST, ticker")
    frame = db.query(con, sql, [INDEX_CODE])
    if limit is not None:
        frame = frame.head(limit)
    return frame


def backfill(con, fetcher, *, limit: int | None = None, include_closed: bool = True,
             progress=None) -> dict:
    """Ingest companies by CIK. One failure never stops the run."""
    from sentinel.ingest import ingest_company

    targets = candidate_tickers(con, include_closed=include_closed, limit=limit)
    ok = failed = 0
    failures: list[tuple[str, str]] = []
    for i, row in enumerate(targets.itertuples(index=False), start=1):
        try:
            ingest_company(con, fetcher, int(row.cik))
            ok += 1
        except Exception as exc:  # recorded, never swallowed
            failed += 1
            failures.append((row.ticker, f"{type(exc).__name__}: {exc}"[:160]))
        if progress is not None:
            progress(i, len(targets), row.ticker, ok, failed)
    return {"attempted": len(targets), "ok": ok, "failed": failed, "failures": failures}


def mark_status(con) -> dict:
    """Classify each company by its index membership, not by optimism.

    'active'        - holds or held an open interval: still a constituent today.
    'former_member' - every interval is closed: it left the index (acquired, delisted,
                      or demoted to a smaller index). All three are real and the
                      warehouse must not pretend otherwise.

    A company that left the index can still be trading, so 'former_member' is not
    'dead'. 'delisted' is reserved for a company with no SEC registrant record at all,
    which this pipeline cannot currently establish; nothing is labelled that way on a
    guess.
    """
    db.run(con, """
        UPDATE dim_company AS c
        SET status = CASE
            WHEN EXISTS (
                SELECT 1 FROM dim_index_membership m
                WHERE m.cik = c.cik AND m.valid_to IS NULL
            ) THEN 'active'
            ELSE 'former_member'
        END
    """)
    counts = db.query(con, "SELECT status, COUNT(*) AS n FROM dim_company GROUP BY status ORDER BY 1")
    return {row["status"]: int(row["n"]) for _, row in counts.iterrows()}


def validate_membership(con) -> dict:
    """Drop ticker->CIK links that the company's own filing history contradicts.

    Tickers are reused. "PMD" was an index member in 1996 and belongs to a different
    company today, so mapping today's PMD onto a 1996 interval invents a relationship
    that never existed. The test applied here is narrow and safe: if a company's first
    ever filing postdates the date it supposedly left the index, it cannot have been a
    member then. Links that fail are nulled, not deleted, so the gap stays visible and
    the interval stays in the universe.
    """
    before = int(db.scalar(con, "SELECT COUNT(*) FROM dim_index_membership WHERE index_code = ? AND cik IS NOT NULL",
                           [INDEX_CODE]) or 0)
    db.run(con, """
        UPDATE dim_index_membership AS m
        SET cik = NULL
        FROM dim_company AS c
        WHERE m.cik = c.cik
          AND m.index_code = ?
          AND m.valid_to IS NOT NULL
          AND c.first_filing_date IS NOT NULL
          AND c.first_filing_date > m.valid_to
    """, [INDEX_CODE])
    after = int(db.scalar(con, "SELECT COUNT(*) FROM dim_index_membership WHERE index_code = ? AND cik IS NOT NULL",
                          [INDEX_CODE]) or 0)
    return {"links_before": before, "links_after": after, "rejected": before - after}
