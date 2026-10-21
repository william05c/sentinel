"""A report context is the exact slice of the warehouse a report is built from.

Both the generator and the verifier open the context the same way, so a number in
the document is always reproducible: rebuild the context for that as-of, re-run that
query, compare the string.
"""

from __future__ import annotations

from datetime import datetime, timezone

import pandas as pd

from sentinel.normalize import canonical

CONTEXT_TABLE = "_canon_rep"


def open_context(con, cik: int, as_of: datetime | None = None) -> pd.DataFrame:
    as_of = as_of or datetime.now(timezone.utc)
    frame = canonical.build_canonical(con, cik, as_of)
    frame = canonical.annual_panel(frame)
    try:
        con.unregister(CONTEXT_TABLE)
    except Exception:
        pass
    con.register(CONTEXT_TABLE, frame)
    return frame


def close_context(con) -> None:
    try:
        con.unregister(CONTEXT_TABLE)
    except Exception:
        pass


def fiscal_years(frame: pd.DataFrame, limit: int = 5) -> list[int]:
    """Years that have at least one annual duration fact.

    Instants alone are not enough: a company that has filed one quarter of 2026 has
    2026 balance-sheet facts but no 2026 income statement. Showing that year would
    produce a column of "n/a" next to real years, which reads as collapse rather
    than as absence.
    """
    if frame.empty:
        return []
    annual = frame[frame["period_type"] == "duration"]
    if annual.empty:
        return []
    years = pd.to_datetime(annual["end_date"]).dt.year.dropna().unique().tolist()
    return sorted((int(y) for y in years), reverse=True)[:limit]
