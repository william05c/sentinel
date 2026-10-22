"""The SEC daily filing index: the change feed for the watch.

One file per day, fixed-width, every filing. It is what makes it unnecessary to poll
500 companies to discover that twelve filed.
"""

from __future__ import annotations

import re
from datetime import date, datetime

import pandas as pd

DAILY_INDEX = "https://www.sec.gov/Archives/edgar/daily-index/{year}/QTR{quarter}/form.{stamp}.idx"


def url_for(day: date) -> str:
    quarter = (day.month - 1) // 3 + 1
    return DAILY_INDEX.format(year=day.year, quarter=quarter, stamp=day.strftime("%Y%m%d"))


def parse_index(text: str) -> pd.DataFrame:
    """Parse a form.idx file into one row per filing.

    Fixed-width slicing was the first implementation and it was wrong on live files:
    the column positions have drifted across eras, the date landed as NaT and the
    accession came out empty, which silently produced zero events. The parser now
    anchors on the date field, which is unambiguous, and reads outward from it.

    Layout: <form> <company> <cik> <YYYY-MM-DD> <path>
    """
    rows = []
    started = False
    for line in text.splitlines():
        if line.startswith("---"):
            started = True
            continue
        if not started or not line.strip():
            continue
        # Two date shapes exist in the wild: "2024-08-01" on recent files and
        # "20240801" on older ones. Matching only one of them silently produced zero rows.
        match = re.search(r"\s(\d{4}-\d{2}-\d{2}|\d{8})\s", line)
        if not match:
            continue
        left, right = line[:match.start()], line[match.end():]
        tail = re.search(r"\s(\d{1,10})\s*$", left)
        if not tail:
            continue
        cik = int(tail.group(1))
        head = left[:tail.start()].rstrip()
        parts = re.split(r"\s{2,}", head, maxsplit=1)
        form = parts[0].strip()
        company = parts[1].strip() if len(parts) > 1 else ""
        filename = right.strip()
        rows.append({
            "form_type": form,
            "company": company,
            "cik": cik,
            "filed_date": _parse_date(match.group(1)),
            "filename": filename,
            "accession": _accession_from(filename),
        })
    return pd.DataFrame(rows, columns=["form_type", "company", "cik", "filed_date", "filename", "accession"])


def _parse_date(raw: str) -> date | None:
    try:
        return date.fromisoformat(raw) if "-" in raw else datetime.strptime(raw, "%Y%m%d").date()
    except ValueError:
        return None


def _accession_from(filename: str) -> str | None:
    """Pull an accession number out of an archive path.

    Two shapes appear in practice: a dashed accession already in the path
    (edgar/data/320193/0000320193-25-000079/...) and an 18-digit directory name
    (000032019325000079). Both are supported; anything else returns None rather than a
    guess, and the caller counts those instead of writing a null key.
    """
    dashed = re.search(r"(\d{10}-\d{2}-\d{6})", filename)
    if dashed:
        return dashed.group(1)
    for part in filename.split("/"):
        if len(part) == 18 and part.isdigit():
            return f"{part[:10]}-{part[10:12]}-{part[12:]}"
    return None


def quarter_label(day: date) -> str:
    return f"{day.year}Q{(day.month - 1) // 3 + 1}"
