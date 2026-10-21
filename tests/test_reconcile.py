"""Reconciliation: our canonical values against SEC's own concept endpoint.

This is the check that stops the pipeline drifting into its own private idea of the
numbers. The concept endpoint is served independently of company facts, so agreeing
with it is evidence rather than tautology.
"""

from __future__ import annotations

import json
from datetime import date
from pathlib import Path

import pandas as pd

from sentinel.normalize import canonical
from sentinel.warehouse import db

FIXTURES = Path(__file__).parent / "fixtures"
CONCEPTS = {
    "Assets": "aapl_concept_Assets.json",
    "Revenues": "aapl_concept_Revenues.json",
    "NetIncomeLoss": "aapl_concept_NetIncomeLoss.json",
}


def concept_values(name: str) -> dict[tuple[str, str], float]:
    payload = json.loads((FIXTURES / CONCEPTS[name]).read_text(encoding="utf-8"))
    values = {}
    for facts in payload["units"].values():
        for row in facts:
            key = (str(row.get("end")), str(row.get("accn")))
            values[key] = float(row["val"])
    return values


def test_canonical_matches_sec_concept(con):
    frame = canonical.build_canonical(con, 320193, datetime_now())
    frame = canonical.annual_panel(frame)

    compared = 0
    mismatches = []
    for name in CONCEPTS:
        reference = concept_values(name)
        rows = frame[frame["tag_used"] == name]
        for _, row in rows.iterrows():
            key = (str(pd.Timestamp(row["end_date"]).date()), str(row["accession"]))
            if key not in reference:
                continue
            compared += 1
            if abs(reference[key] - float(row["value"])) > 0.005:
                mismatches.append((name, key, reference[key], float(row["value"])))

    assert compared >= 10, "reconciliation must compare a meaningful number of facts"
    assert not mismatches, mismatches[:5]


def datetime_now():
    from datetime import datetime, timezone
    return datetime.now(timezone.utc)
