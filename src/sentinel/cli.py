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

import pandas as pd

from sentinel.config import Paths, identity, sources_config
from sentinel.crawler.net import HttpFetcher
from sentinel.ingest import ensure_tag_map, ingest_company, resolve_ticker
from sentinel import universe as universe_mod
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
def universe() -> None:
    """Build the index universe: membership history plus CIK resolution.

    Example: sentinel universe
    """
    paths = _paths()
    con = db.init(paths.db)
    with _fetcher(paths) as fetcher:
        summary = universe_mod.build(con, fetcher)
    table = Table(title="universe")
    table.add_column("measure")
    table.add_column("value", justify="right")
    for key, value in summary.items():
        table.add_row(str(key), f"{value:,}" if isinstance(value, int) else str(value))
    console.print(table)


@app.command()
def backfill(
    limit: int = typer.Option(None, help="max companies to ingest (default: all resolved)"),
    include_closed: bool = typer.Option(True, help="include names that left the index"),
) -> None:
    """Ingest companies from the universe. Resumable: re-running is idempotent.

    Example: sentinel backfill --limit 50
    """
    from rich.progress import Progress

    paths = _paths()
    con = db.init(paths.db)
    ensure_tag_map(con)
    with _fetcher(paths) as fetcher, Progress() as progress:
        task = progress.add_task("ingesting", total=1)

        def report_progress(i: int, total: int, ticker: str, ok: int, failed: int) -> None:
            progress.update(task, total=total, completed=i,
                            description=f"{ticker} ok={ok} failed={failed}")

        stats = universe_mod.backfill(con, fetcher, limit=limit, include_closed=include_closed,
                                     progress=report_progress)
    console.print(f"attempted {stats['attempted']}, ok {stats['ok']}, failed {stats['failed']}")
    for ticker, message in stats["failures"][:10]:
        console.print(f"  [red]{ticker}[/red] {message}")
    console.print(f"validated: {universe_mod.validate_membership(con)}")
    console.print(f"status: {universe_mod.mark_status(con)}")


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
def dq() -> None:
    """Run the data-quality monitors and print what they found.

    Example: sentinel dq
    """
    from sentinel.qa import monitors

    paths = _paths()
    con = db.init(paths.db)
    results = monitors.run(con)
    table = Table(title="data quality")
    table.add_column("check")
    table.add_column("severity")
    table.add_column("detail")
    for row in results:
        table.add_row(row["check_id"], row["severity"], row["detail"])
    console.print(table)


@app.command()
def restatements(limit: int = typer.Option(10, help="rows to show")) -> None:
    """Show values that changed between two filings of the same period.

    Example: sentinel restatements
    """
    from sentinel.normalize import restatements as restated

    paths = _paths()
    con = db.init(paths.db)
    console.print(str(restated.summary(con)))
    frame = restated.detect(con)
    if not frame.empty:
        frame = frame[frame["kind"] == "value_change"].head(limit)
    if frame.empty:
        console.print("no restatements found")
        return
    def _compact(value: float) -> str:
        a = abs(float(value))
        if a >= 1e9:
            return f"{value/1e9:,.2f}B"
        if a >= 1e6:
            return f"{value/1e6:,.2f}M"
        return f"{value:,.0f}"

    table = Table(title="largest restatements")
    for column in ("cik", "tag", "period", "unit", "prior", "restated", "change"):
        table.add_column(column)
    for _, row in frame.iterrows():
        pct = row["pct_change"]
        table.add_row(
            str(row["cik"]), str(row["tag"])[:32], str(row["end_date"])[:10], str(row["unit"])[:8],
            _compact(row["prior_value"]), _compact(row["value"]),
            f"{pct*100:,.2f}%" if pd.notna(pct) else "n/a",
        )
    console.print(table)


@app.command()
def downloads(limit: int = typer.Option(20, help="how many artefacts to list")) -> None:
    """List every generated artefact with its size and content hash.

    Example: sentinel downloads
    """
    from sentinel.reports.render import file_sha256

    paths = _paths()
    files = sorted(
        (f for root in paths.reports.glob("*") if root.is_dir() for f in root.iterdir() if f.is_file()),
        key=lambda f: f.stat().st_mtime,
        reverse=True,
    )[:limit]
    table = Table(title="downloads")
    for column in ("file", "size", "sha256", "modified"):
        table.add_column(column)
    for f in files:
        table.add_row(f.name, f"{f.stat().st_size/1024:,.1f} KB", file_sha256(f)[:12],
                      datetime.fromtimestamp(f.stat().st_mtime).strftime("%Y-%m-%d %H:%M"))
    console.print(table)
    console.print(f"{len(files)} shown · directory {paths.reports}")


@app.command()
def watch(days: int = typer.Option(3, help="how many days back to scan")) -> None:
    """Scan the SEC daily index and write a digest. Quiet when nothing material happened.

    Example: sentinel watch --days 3
    """
    from datetime import date, timedelta

    from sentinel import watch as watch_mod

    paths = _paths()
    con = db.init(paths.db)
    today = date.today()
    span = [today - timedelta(days=i) for i in range(days)]
    with _fetcher(paths) as fetcher:
        stats = watch_mod.sync(con, fetcher, span)
    # Digest the most recent day that actually produced events. Digesting "today" when
    # the index for today is not yet published reports a quiet day that never happened.
    latest = con.execute("SELECT MAX(digest_date) FROM watch_event").fetchone()[0]
    fallback = None
    if stats["days_fetched"] == 0:
        # The index is one request for a whole day; this is one per company. Only when
        # the primary path fails, and only for the companies that need it most.
        with _fetcher(paths) as fetcher:
            fallback = watch_mod.fallback_scan(con, fetcher, limit=40, days=10)
        console.print(f"[yellow]index unavailable — polled submissions for {fallback['polled']} companies, "
                      f"{fallback['new_events']} new filings[/yellow]")
    rescored = watch_mod.rescore(con)
    # Recompute after the fallback: new events carry their own filing dates, and a
    # digest pinned to a date chosen before they existed would hide them.
    latest = con.execute("SELECT MAX(digest_date) FROM watch_event").fetchone()[0]
    d = watch_mod.digest(con, day=latest or today, partial=fallback)
    md, js = watch_mod.write(con, paths, d)
    if stats["failures"]:
        console.print("[yellow]index unavailable:[/yellow] " + "; ".join(stats["failures"][:3]))
    console.print(f"days fetched {stats['days_fetched']}/{stats['days_requested']} · "
                  f"new events {stats['new_events']} · "
                  f"events {rescored['events']:,} surfaced {rescored['surfaced']:,} "
                  f"rescored {rescored['changed']:,}")
    console.print(watch_mod.render(d).strip()[:900])
    console.print(f"written {md}")


@app.command()
def targets(
    ticker: str = typer.Option(..., help="e.g. --ticker AAPL"),
    forms: str = typer.Option("", help="comma-separated form types, e.g. 10-K,10-Q"),
    limit: int = typer.Option(40, help="max filing documents"),
    bulk: bool = typer.Option(True, help="include quarterly SEC bulk archives"),
) -> None:
    """List the actual files available for a company, and write a manifest.

    Example: sentinel targets --ticker AAPL --forms 10-K
    """
    from sentinel import targets as targets_mod

    paths = _paths()
    con = db.init(paths.db)
    wanted = [f.strip() for f in forms.split(",") if f.strip()] or None
    manifest = targets_mod.build_manifest(con, paths, ticker=ticker, forms=wanted,
                                          limit=limit, include_bulk=bulk)
    table = Table(title=f"targets for {ticker.upper()} ({manifest['counts']['filing']} filings, "
                        f"{manifest['counts']['bulk']} bulk)")
    for column in ("form", "filed", "document", "url"):
        table.add_column(column)
    for target in manifest["targets"][:18]:
        table.add_row(target["form"], target["filed"], str(target["document"])[:34], str(target["url"])[:64])
    console.print(table)
    console.print(f"manifest  {paths.root}\\viz\\targets.json")
    console.print("picker    http://127.0.0.1:8777/targets.html")


@app.command()
def download(
    ticker: str = typer.Option(..., help="e.g. --ticker AAPL"),
    forms: str = typer.Option("10-K", help="comma-separated form types"),
    limit: int = typer.Option(3, help="how many documents"),
) -> None:
    """Download the selected filings into data/files/.

    Example: sentinel download --ticker AAPL --forms 10-K --limit 2
    """
    from sentinel import targets as targets_mod

    paths = _paths()
    con = db.init(paths.db)
    wanted = [f.strip() for f in forms.split(",") if f.strip()] or None
    manifest = targets_mod.build_manifest(con, paths, ticker=ticker, forms=wanted,
                                          limit=limit, include_bulk=False)
    with _fetcher(paths) as fetcher:
        saved = targets_mod.download(con, fetcher, paths, manifest, limit=limit)
    for item in saved:
        if "error" in item:
            console.print(f"[red]{item['url'][:70]}[/red] {item['error']}")
        else:
            console.print(f"{item['bytes']:>10,} bytes  {item['path']}")
    console.print(f"saved {sum(1 for s in saved if 'path' in s)} of {len(saved)}")


@app.command()
def firecrawl(url: str = typer.Option(..., help="URL to scrape through Firecrawl")) -> None:
    """Scrape a page through Firecrawl (needs FIRECRAWL_API_KEY).

    Example: sentinel firecrawl --url https://example.com
    """
    from sentinel.adapters import firecrawl as fc

    if not fc.available():
        console.print(fc.INSTALL_HINT)
        raise typer.Exit(code=2)
    result = fc.scrape(url)
    console.print(f"status {result['status']} · {len(result['markdown']):,} chars of markdown")
    console.print(result["markdown"][:2000])


@app.command()
def fetchmd(url: str = typer.Option(..., help="URL to render with the browser backend")) -> None:
    """Render a JavaScript page to markdown. Requires the browser extra.

    Example: sentinel fetchmd --url https://example.com
    """
    from sentinel.crawler import browser

    if not browser.available():
        console.print(browser.INSTALL_HINT)
        raise typer.Exit(code=2)
    rendered = browser.render_sync(url)
    console.print(f"status {rendered.status} · {len(rendered.markdown):,} chars of markdown")
    console.print(rendered.markdown[:2000])


@app.command()
def serve(host: str = typer.Option("127.0.0.1"), port: int = typer.Option(8788),
          reload: bool = typer.Option(False)) -> None:
    """Run the HTTP API. Binds to localhost by default.

    Example: sentinel serve --port 8788
    """
    import uvicorn

    from sentinel.web.app import create_app

    paths = _paths()
    db.init(paths.db).close()  # create the file if needed, then release it
    # The server must not hold the write lock: `sentinel watch` writes to the same file.
    application = create_app(lambda: db.connect(paths.db, read_only=True), paths)
    console.print(f"http://{host}:{port}")
    uvicorn.run(application, host=host, port=port, reload=reload, log_level="warning")


@app.command()
def dashboard() -> None:
    """Write the one-page dashboard over the warehouse.

    Example: sentinel dashboard
    """
    from sentinel.reports import dashboard as dash_mod

    paths = _paths()
    con = db.init(paths.db)
    out = dash_mod.build(con, paths)
    console.print(f"written {out}")
    console.print("served at http://127.0.0.1:8777/dashboard.html")


@app.command()
def findings(
    grade: str = typer.Option(None, help="A, B or C"),
    generate: bool = typer.Option(False, "--generate", help="run the generators first"),
) -> None:
    """The findings ledger: claims with evidence, a grade and a falsifier.

    Example: sentinel findings --generate
    """
    from sentinel.analytics import findings as ledger

    paths = _paths()
    con = db.init(paths.db)
    if generate:
        written = ledger.text_drift_findings(con)
        console.print(f"generated {len(written)} findings from narrative drift")
    console.print(str(ledger.summary(con)))
    frame = ledger.ledger(con, grade=grade, limit=20)
    if frame.empty:
        console.print("ledger is empty")
        return
    table = Table(title="findings")
    for column in ("grade", "title", "effect", "status", "falsifier"):
        table.add_column(column)
    for _, row in frame.iterrows():
        table.add_row(str(row["grade"]), str(row["title"])[:44],
                      f"{row['effect_size']:.2f}" if pd.notna(row["effect_size"]) else "-",
                      str(row["status"]), str(row["falsifier"])[:40])
    console.print(table)


@app.command()
def text(
    ticker: str = typer.Option(..., help="e.g. --ticker AAPL"),
    drift: bool = typer.Option(True, help="compare risk factors with the prior year"),
) -> None:
    """Extract a filing's sections, score their tone, and show what changed.

    Example: sentinel text --ticker AAPL
    """
    from sentinel import document
    from sentinel.adapters import filing_text
    from sentinel.analytics import text as text_mod

    paths = _paths()
    con = db.init(paths.db)
    cik = _resolve_cik(con, paths, ticker, None)
    with _fetcher(paths) as fetcher:
        current = document.ingest_sections(con, fetcher, cik)
        table = Table(title=f"sections for {ticker.upper()} ({current['accession']})")
        for column in ("item", "section", "chars", "neg/1k", "unc/1k", "fog"):
            table.add_column(column)
        rows = db.query(con, "SELECT * FROM dim_document_section WHERE accession = ? ORDER BY section_id",
                        [current["accession"]])
        for _, row in rows.iterrows():
            table.add_row(str(row["section_id"]), str(row["section_name"])[:34],
                          f"{int(row['char_count']):,}", f"{row['sentiment_neg']:.1f}",
                          f"{row['sentiment_unc']:.1f}", f"{row['fog_index']:.1f}")
        console.print(table)

        if not drift:
            return
        filings = db.query(
            con,
            "SELECT accession, filed_date FROM fact_filing WHERE cik = ? AND form_type = '10-K' "
            "ORDER BY filed_date DESC LIMIT 2", [cik],
        )
        if len(filings) < 2:
            console.print("need two annual reports to measure drift")
            return
        prior_accession = filings.iloc[1]["accession"]
        document.ingest_sections(con, fetcher, cik, accession=prior_accession)
        item = "1A" if document.section_text(con, current["accession"], "1A") else "7"
        now_text = document.section_text(con, current["accession"], item) or ""
        then_text = document.section_text(con, prior_accession, item) or ""
        if not now_text or not then_text:
            console.print(f"no Item {item} in both filings")
            return
        drift_rows = text_mod.narrative_drift(now_text, then_text, top=8)
        console.print(f"Item {item} drift · boilerplate {text_mod.boilerplate_ratio(now_text, then_text)*100:.1f}% "
                      f"repeated verbatim")
        drift_table = Table(title="new language")
        for column in ("phrase", "now/1k", "before/1k", "rise"):
            drift_table.add_column(column)
        for item_row in drift_rows:
            drift_table.add_row(item_row["phrase"], str(item_row["now"]), str(item_row["before"]), str(item_row["rise"]))
        console.print(drift_table)


@app.command()
def peers(ticker: str = typer.Option(..., help="e.g. --ticker AAPL"), k: int = typer.Option(10)) -> None:
    """Nearest companies by fingerprint similarity.

    Example: sentinel peers --ticker KO --k 10
    """
    from sentinel.analytics import fingerprint

    paths = _paths()
    con = db.init(paths.db)
    cik = _resolve_cik(con, paths, ticker, None)
    frame = fingerprint.peers(con, cik, k=k)
    table = Table(title=f"peers for {ticker.upper()}")
    for column in ("cik", "fiscal_year", "similarity", "bucket", "net_margin", "roe", "leverage"):
        table.add_column(column)
    for _, row in frame.iterrows():
        table.add_row(str(int(row["cik"])), str(int(row["fiscal_year"])), f"{row['similarity']:.3f}",
                      str(row["bucket"]),
                      f"{row['net_margin']*100:.1f}%" if pd.notna(row["net_margin"]) else "n/a",
                      f"{row['roe']*100:.1f}%" if pd.notna(row["roe"]) else "n/a",
                      f"{row['leverage']:.2f}" if pd.notna(row["leverage"]) else "n/a")
    console.print(table)


@app.command()
def quality(ticker: str = typer.Option(..., help="e.g. --ticker AAPL")) -> None:
    """Accounting-quality scorecard for one company.

    Example: sentinel quality --ticker AAPL
    """
    from sentinel.analytics import fingerprint, quality as quality_mod
    from sentinel.reports.render import apply_format

    paths = _paths()
    con = db.init(paths.db)
    cik = _resolve_cik(con, paths, ticker, None)
    frame = quality_mod.score(fingerprint.panel(con))
    frame = frame[frame["cik"] == cik].sort_values("fiscal_year", ascending=False).head(5)
    table = Table(title=f"quality for {ticker.upper()}")
    for column in ("year", "piotroski", "band", "altman_z2", "z_band", "cash_conv", "accruals"):
        table.add_column(column)
    for _, row in frame.iterrows():
        table.add_row(
            str(int(row["fiscal_year"])),
            f"{row['piotroski_f']:.0f}" if pd.notna(row["piotroski_f"]) else "n/a",
            str(row["piotroski_band"]),
            f"{row['altman_z2']:.2f}" if pd.notna(row["altman_z2"]) else "n/a",
            str(row["altman_band"]),
            f"{row['cash_conversion']:.2f}" if pd.notna(row["cash_conversion"]) else "n/a",
            f"{row['accruals']:.3f}" if pd.notna(row["accruals"]) else "n/a",
        )
    console.print(table)
    for name, caveat in quality_mod.CAVEATS.items():
        console.print(f"[dim]{name}: {caveat}[/dim]")


@app.command()
def precision(k: int = typer.Option(10, help="peer list size")) -> None:
    """Precision@k of the peer fingerprints against hand-labelled pairs.

    Example: sentinel precision --k 10
    """
    from sentinel.analytics import fingerprint

    paths = _paths()
    con = db.init(paths.db)
    pairs = [
        ("KO", "PEP"), ("PEP", "KO"), ("V", "MA"), ("MA", "V"),
        ("XOM", "CVX"), ("CVX", "XOM"), ("HD", "LOW"), ("LOW", "HD"),
        ("INTC", "AMD"), ("AMD", "INTC"), ("JPM", "BAC"), ("BAC", "JPM"),
        ("WMT", "TGT"), ("TGT", "WMT"), ("NEE", "DUK"), ("DUK", "NEE"),
        ("SPG", "PLD"), ("PLD", "SPG"), ("UNH", "CI"), ("CI", "UNH"),
    ]
    labelled: dict[int, int] = {}
    for left, right in pairs:
        lc = _company_cik(con, left)
        rc = _company_cik(con, right)
        if lc and rc:
            labelled[lc] = rc
    result = fingerprint.precision_at_k(con, labelled, k=k)
    console.print(f"pairs {result['pairs']} · hits {result['hits']} · precision@{k} {result['precision_at_k']}")
    for detail in result["details"]:
        if not detail["hit"]:
            console.print(f"  miss: {detail['cik']} expected {detail['expected']} top3 {detail['top']}")
    if result["pairs"] < 10:
        console.print("[yellow]fewer than 10 labelled pairs are loaded; increase the universe first[/yellow]")


@app.command()
def golden(n: int = typer.Option(25, help="companies to reconcile"), years: int = typer.Option(5)) -> None:
    """Reconcile canonical values against SEC's concept endpoint.

    Example: sentinel golden --n 25
    """
    from sentinel.qa import golden as golden_mod

    paths = _paths()
    con = db.init(paths.db)
    with _fetcher(paths) as fetcher:
        result = golden_mod.run(con, fetcher, n=n, years=years)
    path = golden_mod.write_report(result, paths)
    console.print(f"companies {result['companies']} · compared {result['compared']:,} · "
                  f"matched {result['matched']:,} · rate {result['rate']}%")
    console.print(f"written   {path}")
    if result["rate"] < 99.0:
        raise typer.Exit(code=1)


@app.command()
def reparse(limit: int = typer.Option(None, help="max companies to re-parse")) -> None:
    """Re-parse cached filings with the current metric dictionary. No network.

    Example: sentinel reparse
    """
    from sentinel.crawler.net import RawStore
    from sentinel.ingest import reparse_company

    paths = _paths()
    con = db.init(paths.db)
    ensure_tag_map(con)
    store = RawStore(paths)
    ciks = [r[0] for r in con.execute("SELECT DISTINCT cik FROM fact_filing ORDER BY cik").fetchall()]
    if limit:
        ciks = ciks[:limit]
    done = missing = 0
    for cik in ciks:
        try:
            reparse_company(con, store, int(cik))
            done += 1
        except LookupError:
            missing += 1
        if done and done % 50 == 0:
            console.print(f"  re-parsed {done}/{len(ciks)}")
    console.print(f"re-parsed {done}, no cached payload {missing}")


@app.command()
def coverage() -> None:
    """Write a coverage report: companies, metrics, history, unresolved universe.

    Example: sentinel coverage
    """
    from sentinel.qa import coverage as coverage_mod

    paths = _paths()
    con = db.init(paths.db)
    result = coverage_mod.build(con, paths)
    console.print(f"companies {result['companies']:,} · filings {result['filings']:,} · facts {result['facts']:,}")
    console.print("coverage: " + ", ".join(f"{k} {v}%" for k, v in result["coverage"].items()))
    console.print(f"written   {result['path']}")


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
