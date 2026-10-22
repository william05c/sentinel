"""Ingest orchestration: fetch, parse, write, materialise, record.

Every ingest is wrapped in an ingest_run row that records what was attempted and
what was written. When something goes wrong at 2am, that row is the first place
to look, and a run that wrote zero rows is distinguishable from one that never ran.
"""

from __future__ import annotations

import json
import uuid
from datetime import datetime, timezone

import pandas as pd

from sentinel import PARSER_VERSION, SCHEMA_VERSION
from sentinel.adapters import sec
from sentinel.config import metrics_config
from sentinel.crawler.net import RawStore
from sentinel.normalize import canonical
from sentinel.warehouse import db

TICKERS_URL = "https://www.sec.gov/files/company_tickers.json"


def new_run_id(prefix: str = "run") -> str:
    return prefix + "-" + datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S") + "-" + uuid.uuid4().hex[:6]


def _naive(frame: pd.DataFrame) -> pd.DataFrame:
    """DuckDB TIMESTAMP is timezone-naive; strip tz so writes never silently shift."""
    frame = frame.copy()
    for column in frame.columns:
        if str(frame[column].dtype).startswith("datetime64[ns,"):
            frame[column] = frame[column].dt.tz_convert("UTC").dt.tz_localize(None)
    return frame


def ensure_tag_map(con) -> int:
    frame = canonical.tag_map_frame(metrics_config())
    return db.replace_rows(con, "map_tag", frame, keys=["taxonomy", "tag"])


def resolve_ticker(fetcher, ticker: str) -> dict:
    """Map a ticker to a CIK using SEC's own ticker file.

    The file is a snapshot with no history, so this is a convenience, not an
    authority: the warehouse's dim_security is where validity windows live.
    """
    payload, _ = fetcher.get_json(TICKERS_URL, ttl_s=86400)
    wanted = ticker.upper().strip()
    for entry in payload.values():
        if str(entry.get("ticker", "")).upper() == wanted:
            return {"cik": int(entry["cik_str"]), "ticker": entry["ticker"], "name": entry["title"]}
    raise LookupError(f"ticker {wanted} not found in SEC company_tickers.json")


def ingest_company(con, fetcher, cik: int, *, run_id: str | None = None) -> dict:
    """Ingest one company end to end: submissions, facts, canonical, run record."""
    run_id = run_id or new_run_id("ingest")
    started = datetime.now(timezone.utc)
    counts: dict[str, int] = {}
    detail = ""

    try:
        sub_url = sec.SUBMISSIONS_URL.format(cik=cik)
        sub_payload, sub_result = fetcher.get_json(sub_url)
        frames = sec.parse_submissions(
            sub_payload,
            url=sub_url,
            retrieved_at=sub_result.fetched_at,
            document_sha256=sub_result.sha256,
        )
        counts["company"] = db.replace_rows(con, "dim_company", _naive(frames["company"]), ["cik"])
        counts["securities"] = db.replace_rows(
            con, "dim_security", _naive(frames["securities"]), ["cik", "ticker"]
        )
        counts["filings"] = db.replace_rows(con, "fact_filing", _naive(frames["filings"]), ["accession"])

        accepted = {
            row["accession"]: row["acceptance_dt"].to_pydatetime().replace(tzinfo=None)
            for _, row in frames["filings"].iterrows()
            if pd.notna(row["acceptance_dt"])
        }

        facts_url = sec.COMPANYFACTS_URL.format(cik=cik)
        facts_payload, facts_result = fetcher.get_json(facts_url)
        facts = sec.parse_companyfacts(
            facts_payload,
            url=facts_url,
            retrieved_at=facts_result.fetched_at,
            document_sha256=facts_result.sha256,
            run_id=run_id,
            allowed_tags=sec.dictionary_tags(metrics_config()),
            accepted_at_by_accession=accepted,
        )
        db.run(con, "DELETE FROM fact_xbrl WHERE cik = ?", [cik])
        if not facts.empty:
            con.register("_facts", _naive(facts))
            try:
                cols = ", ".join(facts.columns)
                db.run(con, "INSERT INTO fact_xbrl (" + cols + ") SELECT " + cols + " FROM _facts")
            finally:
                con.unregister("_facts")
        counts["xbrl"] = len(facts)
        counts["canonical"] = canonical.materialize(con, cik)

        status = "ok"
    except Exception as exc:  # recorded, then re-raised: a silent failure is worse
        status = "error"
        detail = f"{type(exc).__name__}: {exc}"
        counts = counts or {}
        raise
    finally:
        finished = datetime.now(timezone.utc)
        db.replace_rows(
            con,
            "ingest_run",
            pd.DataFrame([{
                "run_id": run_id,
                "started_at": started.replace(tzinfo=None),
                "finished_at": finished.replace(tzinfo=None),
                "source": "sec_edgar",
                "subject": str(cik),
                "rows_written": int(sum(counts.values())),
                "status": status,
                "detail": detail,
                "parser_version": PARSER_VERSION,
                "schema_version": SCHEMA_VERSION,
            }]),
            ["run_id"],
        )

    counts["run_id"] = run_id  # type: ignore[assignment]
    return counts


def reparse_company(con, store, cik: int, *, run_id: str | None = None) -> int:
    """Re-parse a company's cached company-facts payload with the current dictionary.

    The raw store is immutable and content-addressed, so widening the metric dictionary
    does not require re-downloading 497 companies: the bytes are already on disk and
    are parsed again. This is the payoff for keeping the raw layer instead of parsing
    on the fly.
    """
    url = sec.COMPANYFACTS_URL.format(cik=cik)
    entry = store.lookup(url)
    if entry is None:
        raise LookupError("no cached company facts for CIK " + str(cik))
    payload = json.loads(store.read(entry["sha256"]))
    accepted = {
        row[0]: row[1]
        for row in con.execute(
            "SELECT accession, acceptance_dt FROM fact_filing WHERE cik = ? AND acceptance_dt IS NOT NULL",
            [cik],
        ).fetchall()
    }
    facts = sec.parse_companyfacts(
        payload,
        url=url,
        retrieved_at=datetime.fromisoformat(entry["fetched_at"]).replace(tzinfo=None),
        document_sha256=entry["sha256"],
        run_id=run_id or new_run_id("reparse"),
        allowed_tags=sec.dictionary_tags(metrics_config()),
        accepted_at_by_accession=accepted,
    )
    db.run(con, "DELETE FROM fact_xbrl WHERE cik = ?", [cik])
    if not facts.empty:
        con.register("_facts", _naive(facts))
        try:
            cols = ", ".join(facts.columns)
            db.run(con, "INSERT INTO fact_xbrl (" + cols + ") SELECT " + cols + " FROM _facts")
        finally:
            con.unregister("_facts")
    canonical.materialize(con, cik)
    return len(facts)
