"""The numeric verification gate.

A report is not done when it renders. It is done when every figure in it can be
re-derived from the warehouse and matched, character for character, against what
the document says.

Three failure modes are caught:

1. MISMATCH   - the query now returns a different value than the one printed.
2. LEFTOVER   - a template token was never substituted.
3. UNBOUND    - a number in the prose that no binding accounts for.

The third is the one that matters most, because it is the failure mode of every
report generator that lets a model or a human type a number into a paragraph.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from datetime import date, datetime
from pathlib import Path
from typing import Any

from sentinel.reports import context
from sentinel.reports.render import TOKEN_RE, apply_format

# Numbers that are allowed to appear without a binding: dates, years, accession
# numbers, CIK digits inside URLs, and section ordinals written as words.
# ORDER MATTERS. Accession numbers must be consumed before date patterns run,
# because "0000320193-24-000123" contains a substring that looks like a date
# ("0193-24-00") and stripping that first leaves digit confetti behind.
ALLOWED_PATTERNS = [
    r"(?<![\d-])\d{10}-\d{2}-\d{6}(?![\d-])",  # accession numbers
    r"(?<!\d)\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d+)?(?:Z|[+-]\d{2}:\d{2})?(?!\d)",  # datetimes
    r"(?<!\d)\d{4}-\d{2}-\d{2}(?!\d)",          # ISO dates
    r"(?<![0-9a-f])[0-9a-f]{16}(?![0-9a-f])",   # snapshot hashes
    r"CIK[^0-9\n]{0,8}\d+",                  # CIK identifiers
    r"SIC[^0-9\n]{0,8}\d+",                  # SIC codes
    r"Fiscal year end[^0-9\n]{0,8}\d{4}",    # fiscal year-end codes
    r"\b\d{1,3}-[A-Z]{1,3}(?:/A)?\b",            # form tokens: 10-K, 8-K, 20-F
    r"\b\d{1,3}[A-Z]{1,3}\d{0,2}(?:/A)?\b",      # form tokens: 13F, 424B2
    r"(?<!\d)(?:19|20)\d{2}(?!\d)",          # years, including inside FY2024
    r"\bv\d+\.\d+\.\d+\b",               # versions
]


@dataclass
class Check:
    key: str
    ok: bool
    detail: str = ""
    expected: str | None = None
    found: str | None = None


@dataclass
class Verification:
    ok: bool
    checks: list[Check] = field(default_factory=list)
    unbound: list[str] = field(default_factory=list)

    def summary(self) -> str:
        passed = sum(1 for c in self.checks if c.ok)
        lines = ["verification: " + ("PASS" if self.ok else "FAIL"),
                 "  bindings checked: " + str(len(self.checks)) + " (" + str(passed) + " ok)"]
        for c in self.checks:
            if not c.ok:
                lines.append("  MISMATCH " + c.key + ": expected " + str(c.expected) + " found " + str(c.found))
        if self.unbound:
            lines.append("  UNBOUND numbers: " + ", ".join(self.unbound[:10]))
        return "\n".join(lines)


def _strip_allowed(text: str) -> str:
    for pattern in ALLOWED_PATTERNS:
        text = re.sub(pattern, " ", text)
    return text


def find_unbound(markdown: str, bindings: dict[str, dict]) -> list[str]:
    """Numbers in the prose that no binding explains.

    Bound values are removed by exact string match first. What survives is either
    a date/year/accession (allowed) or a number somebody typed.
    """
    stripped = markdown
    # Remove bound values only where they stand alone. A plain str.replace here is a
    # bug: a value like "722" also occurs inside a snapshot hash, and removing it
    # turns the hash into digit confetti that the scanner then reports.
    for binding in sorted(bindings.values(), key=lambda b: len(str(b.get("rendered", ""))), reverse=True):
        rendered = str(binding.get("rendered", ""))
        if not rendered or rendered == "n/a":
            continue
        pattern = r"(?<![0-9A-Za-z])" + re.escape(rendered) + r"(?![0-9A-Za-z])"
        stripped = re.sub(pattern, " ", stripped)
    # Filing-form cells are structural identifiers, not data. Strip whole cells only.
    # A table cell that is pure identifier (8-K, 10-Q, 424B2, 144, PX14A6G) is a form
    # name, not data. Money and counts in cells always carry $ or thousands separators, so
    # this cannot accidentally strip a real figure.
    stripped = re.sub(r"\|\s*[A-Z0-9][A-Z0-9./-]{0,11}\s*\|", "| |", stripped)
    stripped = _strip_allowed(stripped)
    # fenced code and table pipes are structural, not data
    stripped = re.sub(r"\|", " ", stripped)
    suspects = re.findall(r"(?<![A-Za-z])\d{2,}", stripped)
    return sorted(set(suspects))


def verify_report(con, markdown_path: Path) -> Verification:
    markdown_path = Path(markdown_path)
    stem = markdown_path.with_suffix("")
    bindings_path = stem.with_suffix(".bindings.json")
    provenance_path = stem.with_suffix(".provenance.json")

    if not bindings_path.exists():
        return Verification(ok=False, checks=[Check("bindings", False, "missing " + str(bindings_path))])
    bindings = json.loads(bindings_path.read_text(encoding="utf-8"))
    provenance = json.loads(provenance_path.read_text(encoding="utf-8")) if provenance_path.exists() else {}
    markdown = markdown_path.read_text(encoding="utf-8")

    # Distinct values can repeat across bindings (two years with the same margin), so the
    # occurrence test must be per distinct string, summing the uses of every binding that
    # renders it. Counting per binding would flag legitimate collisions.
    expected_occurrences: dict[str, int] = {}
    for binding in bindings.values():
        rendered_value = str(binding.get("rendered", ""))
        if rendered_value and rendered_value != "n/a":
            expected_occurrences[rendered_value] = (
                expected_occurrences.get(rendered_value, 0) + int(binding.get("uses", 0))
            )

    checks: list[Check] = []
    leftover = TOKEN_RE.findall(markdown)
    checks.append(Check("no_leftover_tokens", not leftover, ",".join(sorted(set(leftover)))))

    cik = provenance.get("cik")
    as_of_raw = provenance.get("as_of")
    if cik is None or as_of_raw is None:
        checks.append(Check("provenance", False, "provenance missing cik/as_of"))
        return Verification(ok=False, checks=checks, unbound=find_unbound(markdown, bindings))

    as_of = datetime.fromisoformat(str(as_of_raw))
    context.open_context(con, int(cik), as_of)
    try:
        for key, binding in bindings.items():
            row = con.execute(binding["sql"], binding["params"]).fetchone()
            value = row[0] if row else None
            expected = "n/a" if value is None else apply_format(binding["fmt"], float(value))
            found = str(binding.get("rendered"))
            ok = expected == found
            detail = ""
            # A value that appears twice (summary and table) must appear twice in the
            # document. Presence alone would let a tampered copy hide behind a good one.
            if ok and found != "n/a":
                occurrences = len(re.findall(
                    r"(?<![0-9A-Za-z])" + re.escape(found) + r"(?![0-9A-Za-z])", markdown))
                uses = expected_occurrences.get(found, occurrences)
                # Every use must still be present. Extra matches are allowed: a count of
                # 101 can legitimately coincide with digits in an unrelated identifier.
                # A missing match is the tamper signature.
                if occurrences < uses:
                    ok = False
                    detail = "appears " + str(occurrences) + " times, expected at least " + str(uses)
            checks.append(Check(key, ok, detail, expected, found))
    finally:
        context.close_context(con)

    unbound = find_unbound(markdown, bindings)
    checks.append(Check("no_unbound_numbers", not unbound, ",".join(unbound[:10])))
    return Verification(ok=all(c.ok for c in checks), checks=checks, unbound=unbound)
