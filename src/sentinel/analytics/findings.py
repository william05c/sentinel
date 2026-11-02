"""The findings ledger.

A finding is a claim with its evidence, its grade and — the part that matters — the
observation that would prove it wrong. The ledger is append-mostly: findings are retired,
never deleted, so the system can show you something it believed and then killed.

Grades:
    A  survives correction for multiple testing, replicates out of sample, economically
       meaningful, robust to neutralisation and subperiod split
    B  statistically significant but sensitive to one of those; hypothesis-worthy
    C  descriptive only; a lead, not a result

Nothing in this project has yet earned a B or an A, and the grading function says so
rather than flattering the output.
"""

from __future__ import annotations

import hashlib
from datetime import datetime, timezone

import pandas as pd

from sentinel import PARSER_VERSION, SCHEMA_VERSION
from sentinel.warehouse import db


def finding_id(title: str, entities: str, period: str) -> str:
    digest = hashlib.sha1(("|".join([title, entities, period])).encode("utf-8")).hexdigest()
    return "f-" + digest[:14]


def record(
    con,
    *,
    title: str,
    claim: str,
    entities: str,
    method: str,
    falsifier: str,
    rerun_command: str,
    sample_period: str = "",
    data_refs: str = "",
    effect_size: float | None = None,
    grade: str = "C",
    n_obs: int | None = None,
    stability: str = "",
    status: str = "new",
) -> str:
    """Write one finding. Re-running produces the same id, so re-derivation is idempotent."""
    fid = finding_id(title, entities, sample_period)
    row = {
        "finding_id": fid,
        "created_at": datetime.now(timezone.utc).replace(tzinfo=None),
        "title": title,
        "claim": claim,
        "entities": entities,
        "sample_period": sample_period,
        "method": method,
        "data_refs": data_refs,
        "effect_size": effect_size,
        "ci_low": None,
        "ci_high": None,
        "p_value": None,
        "q_value": None,
        "n_obs": n_obs,
        "stability": stability,
        "grade": grade.upper(),
        "falsifier": falsifier,
        "status": status,
        "rerun_command": rerun_command,
        "reviewer_note": "",
        "schema_version": SCHEMA_VERSION,
    }
    db.replace_rows(con, "finding", pd.DataFrame([row]), ["finding_id"])
    return fid


def retire(con, finding_id: str, reason: str) -> None:
    """Move a finding to retired. It stays in the table with the note that killed it."""
    db.run(con, "UPDATE finding SET status = 'retired', reviewer_note = ? WHERE finding_id = ?",
           [reason, finding_id])


def ledger(con, *, grade: str | None = None, status: str | None = None, limit: int = 50) -> pd.DataFrame:
    where, params = [], []
    if grade:
        where.append("grade = ?")
        params.append(grade.upper())
    if status:
        where.append("status = ?")
        params.append(status)
    clause = (" WHERE " + " AND ".join(where)) if where else ""
    params.append(limit)
    return db.query(con, "SELECT * FROM finding" + clause + " ORDER BY created_at DESC LIMIT ?", params)


def summary(con) -> dict:
    rows = db.query(con, "SELECT grade, status, COUNT(*) AS n FROM finding GROUP BY grade, status")
    out: dict[str, int] = {}
    for _, row in rows.iterrows():
        out[str(row["grade"]) + "/" + str(row["status"])] = int(row["n"])
    return {"total": int(db.scalar(con, "SELECT COUNT(*) FROM finding") or 0), **out}


DRIFT_THRESHOLD = 1.0   # new-language rise per 1,000 words before it is worth recording


def text_drift_findings(con, *, limit: int = 40) -> list[str]:
    """Turn narrative drift into graded findings.

    Grade C by construction: a phrase rising year over year is a description of the
    document, not evidence about the business. It becomes a lead for a human, which is
    exactly what a C is for.
    """
    from sentinel.analytics import text as text_mod

    companies = db.query(con, """
        SELECT d.accession, f.cik, f.filed_date, d.text
        FROM dim_document_section d
        JOIN fact_filing f ON f.accession = d.accession
        WHERE d.section_id = '1A'
        ORDER BY f.cik, f.filed_date DESC
    """)
    if companies.empty:
        return []

    written: list[str] = []
    for cik, group in companies.groupby("cik"):
        if len(group) < 2:
            continue
        newest, prior = group.iloc[0], group.iloc[1]
        if len(str(newest["text"])) < 2000 or len(str(prior["text"])) < 2000:
            continue
        drift = text_mod.narrative_drift(str(newest["text"]), str(prior["text"]), top=3)
        if not drift or drift[0]["rise"] < DRIFT_THRESHOLD:
            continue
        top = drift[0]
        boilerplate = text_mod.boilerplate_ratio(str(newest["text"]), str(prior["text"]))
        title = "New risk language: " + top["phrase"]
        written.append(record(
            con,
            title=title,
            claim=("Between " + str(prior["filed_date"]) + " and " + str(newest["filed_date"])
                   + ", " + top["phrase"] + " rose from " + str(top["before"]) + " to "
                   + str(top["now"]) + " mentions per thousand words in Item 1A."),
            entities="cik:" + str(int(cik)),
            method="TF trigram rate change in Item 1A, per 1,000 words",
            data_refs=str(newest["accession"]) + "," + str(prior["accession"]),
            sample_period=str(prior["filed_date"]) + ".." + str(newest["filed_date"]),
            effect_size=float(top["rise"]),
            grade="C",
            n_obs=2,
            stability="two filings; " + str(round(boilerplate * 100, 1)) + "% of language repeated verbatim",
            falsifier=("If the same phrase is used at a similar rate in the three filings before "
                       "the prior one, this is drift in my measure, not in the disclosure."),
            rerun_command="sentinel text --ticker <ticker>",
        ))
        if len(written) >= limit:
            break
    return written
