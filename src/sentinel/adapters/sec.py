"""SEC EDGAR adapters: submissions and company facts.

The parsers are pure functions over already-downloaded payloads. That is deliberate:
tests feed them recorded fixtures, so the parser is verifiable without a network,
and re-parsing never needs the SEC to be reachable.
"""

from __future__ import annotations

from datetime import date, datetime, timezone
from typing import Any, Sequence

import pandas as pd

from sentinel import PARSER_VERSION, SCHEMA_VERSION

SUBMISSIONS_URL = "https://data.sec.gov/submissions/CIK{cik:010d}.json"
COMPANYFACTS_URL = "https://data.sec.gov/api/xbrl/companyfacts/CIK{cik:010d}.json"
CONCEPT_URL = "https://data.sec.gov/api/xbrl/companyconcept/CIK{cik:010d}/{taxonomy}/{tag}.json"


def _to_date(value: Any) -> date | None:
    if not value or not isinstance(value, str):
        return None
    try:
        return date.fromisoformat(value[:10])
    except ValueError:
        return None


def _to_ts(value: Any) -> datetime | None:
    if not value or not isinstance(value, str):
        return None
    text = value.replace("Z", "+00:00")
    try:
        ts = datetime.fromisoformat(text)
    except ValueError:
        return None
    return ts if ts.tzinfo else ts.replace(tzinfo=timezone.utc)


def parse_submissions(
    payload: dict,
    *,
    url: str,
    retrieved_at: datetime,
    document_sha256: str,
) -> dict[str, pd.DataFrame]:
    """Split a submissions payload into company, security and filing frames."""
    cik = int(payload["cik"])
    former = payload.get("formerNames") or []
    recent = (payload.get("filings") or {}).get("recent") or {}
    tickers = payload.get("tickers") or []
    exchanges = payload.get("exchanges") or []

    filing_dates = [_to_date(d) for d in recent.get("filingDate", [])]
    non_null = [d for d in filing_dates if d]

    company = pd.DataFrame([{
        "cik": cik,
        "name": payload.get("name") or "",
        "former_names": " | ".join(str(f.get("name") or "") for f in former) or None,
        "entity_type": payload.get("entityType"),
        "sic": payload.get("sic"),
        "sic_description": payload.get("sicDescription"),
        "fiscal_year_end": payload.get("fiscalYearEnd"),
        "state_of_inc": payload.get("stateOfIncorporation"),
        "status": "active",
        "first_filing_date": min(non_null) if non_null else None,
        "last_filing_date": max(non_null) if non_null else None,
        "tickers": ",".join(str(t) for t in tickers) or None,
        "exchanges": ",".join(str(e) for e in exchanges) or None,
        "source_system": "sec_edgar",
        "source_url": url,
        "retrieved_at": retrieved_at,
        "schema_version": SCHEMA_VERSION,
    }])

    securities = pd.DataFrame([{
        "cik": cik,
        "ticker": str(ticker),
        "exchange": str(exchanges[i]) if i < len(exchanges) else None,
        "share_class": None,
        "is_primary": i == 0,
        "valid_from": None,
        "valid_to": None,
        "source_system": "sec_edgar",
        "retrieved_at": retrieved_at,
        "schema_version": SCHEMA_VERSION,
    } for i, ticker in enumerate(tickers)])

    accessions: list[str] = recent.get("accessionNumber", [])
    filings = pd.DataFrame([{
        "accession": acc,
        "cik": cik,
        "form_type": recent["form"][i],
        "filed_date": _to_date(recent["filingDate"][i]),
        "acceptance_dt": _to_ts(recent["acceptanceDateTime"][i]),
        "period_end": _to_date(recent["reportDate"][i]),
        "items": recent["items"][i] or None,
        "document_count": None,
        "primary_document": recent["primaryDocument"][i] or None,
        "primary_desc": recent["primaryDocDescription"][i] or None,
        "is_amendment": str(recent["form"][i]).endswith("/A"),
        "document_sha256": None,
        "source_system": "sec_edgar",
        "source_url": "https://www.sec.gov/Archives/edgar/data/" + str(cik) + "/" + acc.replace("-", ""),
        "retrieved_at": retrieved_at,
        "parser_version": PARSER_VERSION,
        "extractor_run_id": None,
        "schema_version": SCHEMA_VERSION,
    } for i, acc in enumerate(accessions)])

    return {"company": company, "securities": securities, "filings": filings}


def parse_companyfacts(
    payload: dict,
    *,
    url: str,
    retrieved_at: datetime,
    document_sha256: str,
    run_id: str,
    allowed_tags: Sequence[str] | None = None,
    accepted_at_by_accession: dict[str, datetime] | None = None,
) -> pd.DataFrame:
    """Flatten the company-facts JSON into one row per reported fact.

    The allowed_tags filter is a deliberate scope decision, documented in
    LIMITATIONS.md: the full namespace is tens of thousands of facts per company,
    and the canonical layer only consumes dictionary tags. Widening it is a config
    change, not a code change.
    """
    cik = int(payload["cik"])
    accepted_map = accepted_at_by_accession or {}
    allowed = set(allowed_tags) if allowed_tags else None
    rows: list[dict] = []

    for taxonomy, tags in (payload.get("facts") or {}).items():
        for tag, body in tags.items():
            if allowed is not None and tag not in allowed:
                continue
            label = body.get("label")
            for unit, facts in (body.get("units") or {}).items():
                for fact in facts:
                    val = fact.get("val")
                    if val is None:
                        continue
                    end = _to_date(fact.get("end"))
                    start = _to_date(fact.get("start"))
                    accn = fact.get("accn")
                    filed = _to_date(fact.get("filed"))
                    accepted = accepted_map.get(accn) if accn else None
                    if accepted is None and filed is not None:
                        accepted = datetime(filed.year, filed.month, filed.day, tzinfo=timezone.utc)
                    rows.append({
                        "cik": cik,
                        "accession": accn,
                        "taxonomy": taxonomy,
                        "tag": tag,
                        "label": label,
                        "unit": unit,
                        "value": float(val),
                        "start_date": start,
                        "end_date": end,
                        "period_type": "duration" if start is not None else "instant",
                        "fiscal_year": fact.get("fy"),
                        "fiscal_period": fact.get("fp"),
                        "form": fact.get("form"),
                        "filed": filed,
                        "accepted_at": accepted,
                        "frame": fact.get("frame"),
                        "source_system": "sec_edgar",
                        "source_url": url,
                        "retrieved_at": retrieved_at,
                        "document_sha256": document_sha256,
                        "parser_version": PARSER_VERSION,
                        "extractor_run_id": run_id,
                        "schema_version": SCHEMA_VERSION,
                    })

    columns = [
        "cik", "accession", "taxonomy", "tag", "label", "unit", "value", "start_date",
        "end_date", "period_type", "fiscal_year", "fiscal_period", "form", "filed",
        "accepted_at", "frame", "source_system", "source_url", "retrieved_at",
        "document_sha256", "parser_version", "extractor_run_id", "schema_version",
    ]
    return pd.DataFrame(rows, columns=columns)


def parse_companyconcept(payload: dict) -> pd.DataFrame:
    """One concept across time, used to reconcile our values against SEC's own."""
    rows = []
    for unit, facts in (payload.get("units") or {}).items():
        for fact in facts:
            rows.append({
                "tag": payload.get("tag"),
                "unit": unit,
                "value": float(fact["val"]),
                "start_date": _to_date(fact.get("start")),
                "end_date": _to_date(fact.get("end")),
                "accession": fact.get("accn"),
                "form": fact.get("form"),
                "filed": _to_date(fact.get("filed")),
                "frame": fact.get("frame"),
            })
    return pd.DataFrame(rows)


def dictionary_tags(metrics_doc: dict) -> list[str]:
    tags: list[str] = []
    for metric in metrics_doc.get("metrics", []):
        tags.extend(metric.get("tags", []))
    return sorted(set(tags))
