"""A single-page dashboard over the warehouse.

Static HTML, no framework, no CDN: it writes one file that can be served or opened. The
numbers come from SQL, the same queries the CLI uses, so the page cannot drift from the
data it claims to summarise.
"""

from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path

import pandas as pd

from sentinel.warehouse import db

CORE = ["revenue", "net_income", "assets", "equity", "cfo", "capex", "gross_profit", "operating_income"]


def _table(headers: list[str], rows: list[list[str]], numeric_from: int = 1) -> str:
    head = "".join("<th>" + h + "</th>" for h in headers)
    body = []
    for row in rows:
        cells = []
        for i, cell in enumerate(row):
            cls = ' class="n"' if i >= numeric_from else ""
            cells.append("<td" + cls + ">" + str(cell) + "</td>")
        body.append("<tr>" + "".join(cells) + "</tr>")
    return "<table><thead><tr>" + head + "</tr></thead><tbody>" + "".join(body) + "</tbody></table>"


def collect(con) -> dict:
    one = lambda sql, params=None: db.scalar(con, sql, params)
    stats = {
        "companies": int(one("SELECT COUNT(*) FROM dim_company") or 0),
        "filings": int(one("SELECT COUNT(*) FROM fact_filing") or 0),
        "facts": int(one("SELECT COUNT(*) FROM fact_xbrl") or 0),
        "canonical": int(one("SELECT COUNT(*) FROM fact_xbrl_canonical") or 0),
        "findings": int(one("SELECT COUNT(*) FROM finding") or 0),
        "sections": int(one("SELECT COUNT(*) FROM dim_document_section") or 0),
        "events": int(one("SELECT COUNT(*) FROM watch_event") or 0),
    }
    coverage = db.query(con, "SELECT metric_id, COUNT(DISTINCT cik) AS n FROM fact_xbrl_canonical "
                             "WHERE metric_id IN (" + ",".join("?" * len(CORE)) + ") GROUP BY 1 ORDER BY 1",
                        CORE)
    coverage_rows = [[r["metric_id"], f"{int(r['n']):,}", f"{r['n'] / max(1, stats['companies']) * 100:.1f}%"]
                     for _, r in coverage.iterrows()]

    quality = db.query(con, "SELECT check_id, severity, detail FROM dq_issue ORDER BY severity, check_id LIMIT 12")
    quality_rows = [[r["check_id"], str(r["severity"]), str(r["detail"] or "")[:60]] for _, r in quality.iterrows()]

    findings = db.query(con, "SELECT grade, title, effect_size, status FROM finding "
                             "ORDER BY created_at DESC LIMIT 10")
    finding_rows = [[r["grade"], str(r["title"])[:60],
                     f"{r['effect_size']:.2f}" if pd.notna(r["effect_size"]) else "-", r["status"]]
                    for _, r in findings.iterrows()]

    latest = db.scalar(con, "SELECT MAX(digest_date) FROM watch_event")
    events = pd.DataFrame()
    if latest is not None:
        events = db.query(con, "SELECT e.form_type, c.name, e.reason, e.weight FROM watch_event e "
                               "LEFT JOIN dim_company c ON c.cik = e.cik "
                               "WHERE e.digest_date = ? AND e.surfaced ORDER BY e.weight DESC LIMIT 8", [latest])
    event_rows = [[r["form_type"], str(r["name"] or "")[:38], str(r["reason"])[:44], f"{r['weight']:.2f}"]
                  for _, r in events.iterrows()]

    arts = db.query(con, "SELECT kind, subject, as_of, artifact_paths FROM report_run ORDER BY generated_at DESC LIMIT 8")
    art_rows = [[r["kind"], str(r["subject"]), str(r["as_of"])] for _, r in arts.iterrows()]
    return {"stats": stats, "coverage": coverage_rows, "quality": quality_rows,
            "findings": finding_rows, "events": event_rows, "artifacts": art_rows, "latest": latest}


def render(data: dict) -> str:
    s = data["stats"]
    cards = "".join(
        f'<div class="card"><div class="k">{k}</div><div class="v">{v:,}</div></div>'
        for k, v in [("companies", s["companies"]), ("filings", s["filings"]),
                     ("xbrl facts", s["facts"]), ("canonical", s["canonical"]),
                     ("sections", s["sections"]), ("events", s["events"]),
                     ("findings", s["findings"])]
    )
    return f"""<!DOCTYPE html><html lang="en"><head><meta charset="utf-8">
<title>SENTINEL — dashboard</title>
<style>
 :root{{--bg:#070a0e;--panel:#0d141c;--line:#18222e;--ink:#a8bccf;--dim:#5f7284;--lit:#eaf4ff;--ok:#3ddc97}}
 *{{box-sizing:border-box}} body{{margin:0;background:var(--bg);color:var(--ink);
   font:13px/1.55 ui-monospace,SFMono-Regular,Consolas,monospace;padding:26px}}
 h1{{font-size:12px;letter-spacing:.22em;text-transform:uppercase;color:var(--lit);margin:0 0 4px}}
 .sub{{color:var(--dim);font-size:11px;margin-bottom:20px}}
 .cards{{display:flex;gap:10px;flex-wrap:wrap;margin-bottom:22px}}
 .card{{background:var(--panel);border:1px solid var(--line);border-radius:8px;padding:12px 16px;min-width:120px}}
 .card .k{{color:var(--dim);font-size:9px;letter-spacing:.14em;text-transform:uppercase}}
 .card .v{{color:var(--lit);font-size:20px}}
 section{{margin-bottom:26px}}
 h2{{font-size:10px;letter-spacing:.18em;text-transform:uppercase;color:var(--dim);
   border-bottom:1px solid var(--line);padding-bottom:6px;margin:0 0 10px}}
 table{{width:100%;border-collapse:collapse}}
 th,td{{text-align:left;padding:6px 10px;border-bottom:1px solid #111a23;font-size:12px}}
 th{{color:var(--dim);font-weight:500;font-size:10px;letter-spacing:.1em;text-transform:uppercase}}
 td.n{{text-align:right;font-variant-numeric:tabular-nums}}
 .grade{{color:var(--ok);font-weight:600}}
 a{{color:var(--ok);text-decoration:none}}
 footer{{color:var(--dim);font-size:10px;border-top:1px solid var(--line);padding-top:12px}}
</style></head><body>
<h1>SENTINEL — dashboard</h1>
<div class="sub">generated {datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M UTC')} · latest digest {data['latest'] or 'none'} ·
 <a href="/crawler.html">crawler view</a> · <a href="/provenance.html">provenance</a></div>
<div class="cards">{cards}</div>
<section><h2>Metric coverage</h2>{_table(["metric", "companies", "share"], data["coverage"])}</section>
<section><h2>Data quality</h2>{_table(["check", "severity", "detail"], data["quality"])}</section>
<section><h2>Findings ledger</h2>{_table(["grade", "title", "effect", "status"], data["findings"])}</section>
<section><h2>Latest surfaced filings</h2>{_table(["form", "company", "why", "weight"], data["events"])}</section>
<section><h2>Recent artefacts</h2>{_table(["kind", "subject", "as of"], data["artifacts"])}</section>
<footer>Research output generated from public data. Not investment advice.</footer>
</body></html>"""


def build(con, paths) -> Path:
    data = collect(con)
    html = render(data)
    out = Path(paths.root) / "viz" / "dashboard.html"
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(html, encoding="utf-8")
    record = Path(paths.reports) / "index.html"
    record.parent.mkdir(parents=True, exist_ok=True)
    record.write_text(html, encoding="utf-8")
    return out
