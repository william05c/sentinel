"""The watch: detect what changed and decide what deserves attention.

A digest is only useful if it is short, so materiality is a scoring subsystem rather
than a sort order. Two rules keep it honest:

* Quiet by default. A day with nothing material produces one line saying so.
* Every surfaced item carries a reason. An alert that cannot explain itself is noise.

Scope limit, stated plainly: scoring uses the form type from the daily index. Item-level
8-K classification (2.02 results, 4.02 non-reliance) needs the filing's own metadata and
is not wired in yet; those events score by form only.
"""

from __future__ import annotations

import json
import re
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

import pandas as pd

from sentinel import SCHEMA_VERSION
from sentinel.adapters import sec_index
from sentinel.warehouse import db

# Weights by form type. Tunable in config/events.yaml; these are the defaults.
WEIGHTS: dict[str, tuple[float, str]] = {
    "10-K": (0.55, "annual report"),
    "10-Q": (0.35, "quarterly report"),
    "8-K": (0.60, "current report"),
    "8-K/A": (0.45, "amended current report"),
    "NT 10-K": (0.95, "late annual filing notice"),
    "NT 10-Q": (0.90, "late quarterly filing notice"),
    "4": (0.12, "insider transaction"),
    "4/A": (0.10, "amended insider transaction"),
    "SC 13D": (0.85, "activist stake"),
    "SC 13D/A": (0.70, "activist stake amended"),
    "SC 13G": (0.45, "passive stake"),
    "DEF 14A": (0.40, "proxy statement"),
    "S-1": (0.60, "registration for new shares"),
    "424B4": (0.45, "prospectus filed"),
    "15-12G": (0.85, "deregistration"),
    "25-NSE": (0.95, "delisting notice"),
}

THRESHOLD = 0.30   # below this an event is recorded but not surfaced
BUDGET = 25        # max surfaced items per digest

# 8-K item codes. Without these every current report scores the same and the digest
# cannot tell a routine earnings release from a restatement. The codes come from the
# submissions payload, which we already store on fact_filing.
ITEM_WEIGHTS: dict[str, tuple[float, str]] = {
    "1.01": (0.70, "material definitive agreement"),
    "1.02": (0.65, "termination of a material agreement"),
    "1.05": (0.95, "cybersecurity incident"),
    "2.01": (0.80, "completion of an acquisition or disposal"),
    "2.02": (0.62, "results of operations"),
    "2.05": (0.70, "exit or disposal activities"),
    "2.06": (0.82, "material impairment"),
    "3.01": (0.88, "delisting or listing-standard failure"),
    "4.01": (0.60, "change in certifying accountant"),
    "4.02": (0.98, "non-reliance on previously issued financials"),
    "5.01": (0.70, "change in control"),
    "5.02": (0.75, "officer or director change"),
    "7.01": (0.35, "regulation FD disclosure"),
    "8.01": (0.40, "other events"),
    "9.01": (0.15, "financial statements and exhibits"),
}

ITEM_RE = re.compile(r"\b\d\.\d{2}\b")


def score(form_type: str) -> tuple[float, str]:
    return WEIGHTS.get(form_type.strip().upper(), (0.20, "other filing"))


def score_items(form_type: str, items: str | None) -> tuple[float, str]:
    """Score an 8-K by its item codes, falling back to the form when they are absent.

    A filing's items are the difference between "Apple reported quarterly results" and
    "Apple says its prior financials cannot be relied on". Both are 8-Ks; only one
    deserves to wake you.
    """
    # A LEFT JOIN yields NaN for events whose filing metadata has not been ingested yet;
    # NaN is a float, and the regex will not take it.
    if not items or not isinstance(items, str):
        return score(form_type)
    best: tuple[float, str] | None = None
    for code in ITEM_RE.findall(items):
        found = ITEM_WEIGHTS.get(code)
        if found and (best is None or found[0] > best[0]):
            best = (found[0], code + " " + found[1])
    return best or score(form_type)


def ensure_schema(con) -> None:
    db.run(con, """
        CREATE TABLE IF NOT EXISTS watch_event (
            accession     VARCHAR,
            event_class   VARCHAR,
            cik           BIGINT,
            ticker        VARCHAR,
            form_type     VARCHAR,
            filed_date    DATE,
            weight        DOUBLE,
            reason        VARCHAR,
            detected_at   TIMESTAMP,
            digest_date   DATE,
            surfaced      BOOLEAN,
            schema_version INTEGER,
            PRIMARY KEY (accession, event_class)
        )
    """)


def universe_ciks(con) -> set[int]:
    return {int(r[0]) for r in con.execute("SELECT DISTINCT cik FROM dim_company").fetchall()}


def sync(con, fetcher, days: list[date], *, quiet: bool = True) -> dict:
    """Fetch the daily index for each day, keep filings from our universe, record events."""
    ensure_schema(con)
    known_ciks = universe_ciks(con)
    if not known_ciks:
        raise RuntimeError("no companies in the warehouse; run 'sentinel backfill' first")

    new_events = 0
    fetched = 0
    skipped = 0
    failures: list[str] = []
    for day in days:
        url = sec_index.url_for(day)
        try:
            result = fetcher.fetch(url)
        except Exception as exc:
            skipped += 1
            failures.append(str(day) + ": " + type(exc).__name__ + " " + str(exc)[:120])
            continue
        fetched += 1
        frame = sec_index.parse_index(result.text)
        if frame.empty:
            continue
        frame = frame[frame["cik"].isin(known_ciks)]
        # An accession derived from the archive path can be missing for a handful of
        # odd filings; the event key needs it, so they are counted and skipped rather
        # than written with a null primary key.
        before_rows = len(frame)
        frame = frame[frame["accession"].notna()]
        skipped += before_rows - len(frame)
        if frame.empty:
            continue

        # Items live on the filing metadata, joined by accession. Filings newer than the
        # last ingest have no items yet and fall back to the form weight.
        items_by_accession = {
            r[0]: r[1]
            for r in con.execute(
                "SELECT accession, items FROM fact_filing WHERE cik IN (SELECT cik FROM dim_company) "
                "AND filed_date >= ?", [day]
            ).fetchall()
        }

        rows = []
        for _, row in frame.iterrows():
            weight, reason = score_items(row["form_type"], items_by_accession.get(row["accession"]))
            rows.append({
                "accession": row["accession"], "event_class": row["form_type"],
                "cik": int(row["cik"]), "ticker": None, "form_type": row["form_type"],
                "filed_date": row["filed_date"], "weight": weight, "reason": reason,
                "detected_at": datetime.now(timezone.utc).replace(tzinfo=None),
                "digest_date": day, "surfaced": weight >= THRESHOLD,
                "schema_version": SCHEMA_VERSION,
            })
        if not rows:
            continue
        before = int(db.scalar(con, "SELECT COUNT(*) FROM watch_event") or 0)
        db.replace_rows(con, "watch_event", pd.DataFrame(rows), ["accession", "event_class"])
        after = int(db.scalar(con, "SELECT COUNT(*) FROM watch_event") or 0)
        new_events += max(0, after - before)

    return {"days_requested": len(days), "days_fetched": fetched, "days_unavailable": skipped,
            "new_events": new_events, "failures": failures}


SUBMISSIONS_URL = "https://data.sec.gov/submissions/CIK{cik:010d}.json"


def fallback_scan(con, fetcher, *, limit: int = 40, days: int = 10) -> dict:
    """Detect new filings through the submissions API when the daily index is blocked.

    The index is one request for every filing in a day; this is one request per company.
    That is 500x more expensive, which is exactly why it is the fallback and not the
    primary path. It polls the companies with the stalest filing history first, so a
    limited budget still covers the whole universe over several runs.
    """
    ensure_schema(con)
    cutoff = date.today() - timedelta(days=days)
    targets = db.query(con, """
        SELECT c.cik, MAX(v.filed_date) AS last_filed
        FROM dim_company c LEFT JOIN fact_filing v ON v.cik = c.cik
        WHERE c.status = 'active'
        GROUP BY c.cik
        ORDER BY last_filed NULLS FIRST
        LIMIT ?
    """, [limit])

    polled = 0
    fresh = 0
    failures: list[str] = []
    for cik in targets["cik"].tolist():
        try:
            payload, _ = fetcher.get_json(SUBMISSIONS_URL.format(cik=int(cik)))
            polled += 1
        except Exception as exc:
            failures.append(str(cik) + ": " + type(exc).__name__)
            continue
        recent = (payload.get("filings") or {}).get("recent") or {}
        for i, accession in enumerate(recent.get("accessionNumber", [])):
            filed = recent.get("filingDate", [None] * (i + 1))[i]
            if not filed:
                continue
            try:
                filed_date = date.fromisoformat(filed)
            except ValueError:
                continue
            if filed_date < cutoff:
                continue
            exists = con.execute("SELECT 1 FROM watch_event WHERE accession = ?", [accession]).fetchone()
            if exists:
                continue
            form = recent.get("form", [None] * (i + 1))[i]
            items = recent.get("items", [""] * (i + 1))[i]
            weight, reason = score_items(form, items)
            db.run(con, """
                INSERT INTO watch_event VALUES (?, ?, ?, NULL, ?, ?, ?, ?, ?, ?, ?, ?)
            """, [accession, form, int(cik), form, filed_date, weight, reason,
                  datetime.now(timezone.utc).replace(tzinfo=None), filed_date,
                  weight >= THRESHOLD, SCHEMA_VERSION])
            fresh += 1
    return {"polled": polled, "new_events": fresh, "failures": failures}


def rescore(con) -> dict:
    """Re-derive weights and surfacing for events already recorded.

    Item-level scoring needs the filing metadata, which lands with ingest; an event
    captured before that, or captured while the index was blocked, can still be scored
    correctly once the metadata exists. No network involved.
    """
    ensure_schema(con)
    rows = db.query(con, """
        SELECT e.accession, e.form_type, f.items
        FROM watch_event e LEFT JOIN fact_filing f ON f.accession = e.accession
    """)
    changed = 0
    for _, row in rows.iterrows():
        weight, reason = score_items(row["form_type"], row["items"])
        surfaced = bool(weight >= THRESHOLD)
        before = con.execute(
            "SELECT weight, surfaced FROM watch_event WHERE accession = ?", [row["accession"]]
        ).fetchall()
        if not before:
            continue
        if abs(before[0][0] - weight) > 1e-9 or bool(before[0][1]) != surfaced:
            changed += 1
        db.run(con, "UPDATE watch_event SET weight = ?, reason = ?, surfaced = ? WHERE accession = ?",
               [weight, reason, surfaced, row["accession"]])
    total = int(db.scalar(con, "SELECT COUNT(*) FROM watch_event") or 0)
    surfaced = int(db.scalar(con, "SELECT COUNT(*) FROM watch_event WHERE surfaced") or 0)
    return {"events": total, "surfaced": surfaced, "changed": changed}


def digest(con, *, day: date | None = None, limit: int = BUDGET, partial: dict | None = None) -> dict:
    """Build the digest for a day. Quiet by default."""
    ensure_schema(con)
    day = day or date.today()
    frame = db.query(con, """
        SELECT e.accession, e.form_type, e.weight, e.reason, e.cik, e.filed_date,
               c.name, c.status
        FROM watch_event e LEFT JOIN dim_company c ON c.cik = e.cik
        WHERE e.digest_date = ? AND e.surfaced
        ORDER BY e.weight DESC, e.cik
    """, [day])
    suppressed = int(db.scalar(con, "SELECT COUNT(*) FROM watch_event WHERE digest_date = ? AND NOT surfaced", [day]) or 0)
    items = frame.head(limit).to_dict("records")
    return {
        "date": day.isoformat(),
        "items": items,
        "suppressed": suppressed,
        "truncated": max(0, len(frame) - limit),
        "quiet": len(items) == 0,
        # A quiet day from a partial scan is not the same claim as a quiet day from a
        # full one, so the difference travels with the digest.
        "partial": partial,
    }


def render(d: dict) -> str:
    partial = d.get("partial")
    caveat = ""
    if partial:
        caveat = ("\n_Scan coverage: the daily index was unavailable, so this digest reflects "
                  + str(partial.get("polled", 0)) + " companies polled directly. "
                  + "Silence here is weaker evidence than silence from a full index scan._\n")
    if d["quiet"]:
        return (f"# SENTINEL digest — {d['date']}\n\nNothing material changed. "
                f"{d['suppressed']} routine filings were suppressed.\n" + caveat)
    lines = [f"# SENTINEL digest — {d['date']}", "",
             f"{len(d['items'])} material items, {d['suppressed']} suppressed.", ""]
    if caveat:
        lines.append(caveat.strip())
        lines.append("")
    for item in d["items"]:
        ticker = item.get("name") or str(item["cik"])
        filed = str(item.get("filed_date") or "")[:10]
        accession = str(item.get("accession") or "")[-6:]
        # Two 8-Ks from the same company on the same day are different filings, not a
        # duplicate row; the accession tail is what distinguishes them at a glance.
        lines.append(f"- **{item['form_type']}** · {ticker} — {item['reason']} "
                     f"(weight {item['weight']:.2f}, filed {filed}, acc …{accession})")
    if d["truncated"]:
        lines.append(f"- and {d['truncated']} more above the alert budget")
    lines += ["", "Research output generated from public data. Not investment advice.", ""]
    return "\n".join(lines)


def write(con, paths, d: dict) -> tuple[Path, Path]:
    out_dir = Path(paths.reports) / d["date"]
    out_dir.mkdir(parents=True, exist_ok=True)
    md = out_dir / (d["date"] + "_digest.md")
    js = out_dir / (d["date"] + "_digest.json")
    md.write_text(render(d), encoding="utf-8")
    js.write_text(json.dumps(d, indent=1, default=str), encoding="utf-8")
    return md, js
