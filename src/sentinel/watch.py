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


def score(form_type: str) -> tuple[float, str]:
    return WEIGHTS.get(form_type.strip().upper(), (0.20, "other filing"))


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
    for day in days:
        url = sec_index.url_for(day)
        try:
            result = fetcher.fetch(url)
        except Exception:
            skipped += 1
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

        rows = []
        for _, row in frame.iterrows():
            weight, reason = score(row["form_type"])
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
            "new_events": new_events}


def digest(con, *, day: date | None = None, limit: int = BUDGET) -> dict:
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
    }


def render(d: dict) -> str:
    if d["quiet"]:
        return f"# SENTINEL digest — {d['date']}\n\nNothing material changed. " \
               f"{d['suppressed']} routine filings were suppressed.\n"
    lines = [f"# SENTINEL digest — {d['date']}", "",
             f"{len(d['items'])} material items, {d['suppressed']} suppressed.", ""]
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
