"""Warehouse connection and write helpers.

Writes are idempotent by construction: every ingest replaces the rows it is about
to insert, keyed on a natural key. That is what makes "run it twice, get the same
row counts and hashes" a property of the system rather than a hope.
"""

from __future__ import annotations

from pathlib import Path
from typing import Iterable, Sequence

import duckdb
import pandas as pd

SCHEMA_PATH = Path(__file__).with_name("schema.sql")


def connect(db_path: Path | str = ":memory:", *, read_only: bool = False) -> duckdb.DuckDBPyConnection:
    path = str(db_path)
    if path != ":memory:":
        Path(path).parent.mkdir(parents=True, exist_ok=True)
    return duckdb.connect(path, read_only=read_only)


def init(db_path: Path | str) -> duckdb.DuckDBPyConnection:
    """Connect and apply the schema. Safe to call repeatedly."""
    con = connect(db_path)
    con.execute(SCHEMA_PATH.read_text(encoding="utf-8"))
    return con


def replace_rows(
    con: duckdb.DuckDBPyConnection,
    table: str,
    frame: pd.DataFrame,
    keys: Sequence[str],
) -> int:
    """Replace rows in `table` matching `frame` on `keys`, then insert `frame`.

    An empty frame is a no-op. A source that legitimately returns nothing must not
    delete what is already stored - that is how a transient outage becomes data loss.
    """
    if frame is None or frame.empty:
        return 0
    missing = [k for k in keys if k not in frame.columns]
    if missing:
        raise KeyError(f"{table}: key columns missing from frame: {missing}")

    con.register("_incoming", frame)
    try:
        cols = ", ".join(chr(34) + c + chr(34) for c in frame.columns)
        join = " AND ".join("t." + chr(34) + k + chr(34) + " = i." + chr(34) + k + chr(34) for k in keys)
        run(con,
            "DELETE FROM " + table + " AS t WHERE EXISTS "
            "(SELECT 1 FROM _incoming AS i WHERE " + join + ")")
        run(con, "INSERT INTO " + table + " (" + cols + ") SELECT " + cols + " FROM _incoming")
    finally:
        con.unregister("_incoming")
    return len(frame)


def run(con: duckdb.DuckDBPyConnection, sql: str, params: Iterable | None = None) -> None:
    """Execute a statement whose result we do not want, and consume it.

    DuckDB keeps a result handle open until it is drained. An un-drained DELETE makes
    the next statement on the same connection fail with ResourceInUseException, which
    is how a second ingest in one process breaks while the first looks fine.
    """
    con.execute(sql, list(params) if params is not None else None).fetchall()


def query(con: duckdb.DuckDBPyConnection, sql: str, params: Iterable | None = None) -> pd.DataFrame:
    return con.execute(sql, list(params) if params is not None else None).fetch_df()


def scalar(con: duckdb.DuckDBPyConnection, sql: str, params: Iterable | None = None):
    """First column of the first row, with the whole result drained.

    fetchone() leaves the remaining result open, and an open result blocks the next
    statement on the same connection. This is the single most common way a DuckDB
    session deadlocks itself.
    """
    rows = con.execute(sql, list(params) if params is not None else None).fetchall()
    return rows[0][0] if rows else None


def table_counts(con: duckdb.DuckDBPyConnection) -> dict[str, int]:
    tables = [r[0] for r in con.execute(
        "SELECT table_name FROM information_schema.tables "
        "WHERE table_schema = 'main' ORDER BY 1"
    ).fetchall()]
    return {t: int(scalar(con, 'SELECT COUNT(*) FROM "' + t + '"') or 0) for t in tables}
