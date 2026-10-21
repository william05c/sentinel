"""Command line interface.

Every command is designed to be runnable a year from now by someone who has never
seen this repository: the help text says what it does and gives an example.
"""

from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path

import typer
from rich.console import Console
from rich.table import Table

from sentinel.config import Paths, identity, sources_config
from sentinel.crawler.net import HttpFetcher
from sentinel.ingest import ensure_tag_map, ingest_company, resolve_ticker
from sentinel.reports import dossier
from sentinel.verify import verify_report
from sentinel.warehouse import db

app = typer.Typer(add_completion=False, help="SENTINEL — point-in-time public-company intelligence.")
console = Console()

KIND_ALIASES = {"dossier": "dossier"}


def _paths() -> Paths:
    paths = Paths.build()
    paths.ensure()
    return paths


def _fetcher(paths: Paths) -> HttpFetcher:
    _, defaults = sources_config()
    return HttpFetcher(paths, identity(), defaults)


def _company_cik(con, ticker: str) -> int | None:
    row = con.execute(
        "SELECT cik FROM dim_security WHERE upper(ticker) = upper(?) LIMIT 1", [ticker]
    ).fetchone()
    if row:
        return int(row[0])
    row = con.execute(
        "SELECT cik FROM dim_company WHERE ',' || tickers || ',' LIKE '%,' || upper(?) || ',%' LIMIT 1",
        [ticker],
    ).fetchone()
    return int(row[0]) if row else None


def _resolve_cik(con, paths: Paths, ticker: str | None, cik: int | None) -> int:
    if cik is not None:
        return int(cik)
    if not ticker:
        raise typer.BadParameter("provide --ticker or --cik")
    known = _company_cik(con, ticker)
    if known is not None:
        return known
    with _fetcher(paths) as fetcher:
        resolved = resolve_ticker(fetcher, ticker)
    console.print(f"[dim]resolved[/dim] {ticker} -> CIK {resolved['cik']} ({resolved['name']})")
    return int(resolved["cik"])


@app.command()
def init() -> None:
    """Create the warehouse and load the metric dictionary. Safe to re-run."""
    paths = _paths()
    con = db.init(paths.db)
    count = ensure_tag_map(con)
    console.print(f"warehouse: {paths.db}")
    console.print(f"metric dictionary: {count} tag mappings")
    console.print(str(db.table_counts(con)))


@app.command()
def ingest(
    ticker: str = typer.Option(None, help="e.g. --ticker AAPL"),
    cik: int = typer.Option(None, help="e.g. --cik 320193"),
) -> None:
    """Fetch one company's filings and XBRL facts. Idempotent."""
    paths = _paths()
    con = db.init(paths.db)
    ensure_tag_map(con)
    target = _resolve_cik(con, paths, ticker, cik)
    with _fetcher(paths) as fetcher:
        counts = ingest_company(con, fetcher, target)
    table = Table(title=f"ingested CIK {target}")
    table.add_column("table")
    table.add_column("rows", justify="right")
    for key in ("company", "securities", "filings", "xbrl", "canonical"):
        table.add_row(key, str(counts.get(key, 0)))
    table.add_row("run_id", str(counts.get("run_id", "")))
    console.print(table)


@app.command()
def report(
    kind: str = typer.Argument("dossier", help="report kind (dossier)"),
    ticker: str = typer.Option(None, help="e.g. --ticker AAPL"),
    cik: int = typer.Option(None, help="e.g. --cik 320193"),
    as_of: str = typer.Option(None, help="ISO date or datetime; defaults to now"),
    years: int = typer.Option(5, help="fiscal years to show"),
) -> None:
    """Render a report and write markdown, HTML, bindings and provenance."""
    if kind not in KIND_ALIASES:
        raise typer.BadParameter(f"unknown report kind: {kind} (known: {sorted(KIND_ALIASES)})")
    paths = _paths()
    con = db.init(paths.db)
    target = _resolve_cik(con, paths, ticker, cik)
    when = datetime.fromisoformat(as_of) if as_of else datetime.now(timezone.utc)
    if when.tzinfo is None:
        when = when.replace(tzinfo=timezone.utc)
    result = dossier.build(con, paths, target, as_of=when, years=years)
    for name, path in result["artifacts"].items():
        console.print(f"{name:11s} {path}")
    console.print(f"snapshot    {result['provenance']['snapshot_hash']}")


@app.command()
def verify(path: str = typer.Argument(..., help="path to the report .md file")) -> None:
    """Re-derive every number in a report. Exits 1 if anything does not match."""
    paths = _paths()
    con = db.connect(paths.db)
    result = verify_report(con, Path(path))
    console.print(result.summary())
    if not result.ok:
        raise typer.Exit(code=1)


@app.command()
def doctor() -> None:
    """Show what the environment can and cannot do. Read-only."""
    paths = _paths()
    ident = identity()
    console.print(f"root          {paths.root}")
    console.print(f"warehouse     {paths.db} (exists: {paths.db.exists()})")
    console.print(f"user agent    {ident.header if ident.contact else ident.user_agent + ' (no contact configured!)'}")
    cfg, _ = sources_config()
    table = Table(title="sources")
    table.add_column("source")
    table.add_column("hosts")
    table.add_column("rps", justify="right")
    table.add_column("enabled")
    for spec in cfg["specs"].values():  # type: ignore[index]
        table.add_row(spec.name, ",".join(spec.hosts), str(spec.requests_per_second), str(spec.enabled))
    console.print(table)


if __name__ == "__main__":
    app()
