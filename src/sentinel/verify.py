"""The numeric verification gate.

A report is not done when it renders. It is done when every figure in it can be
re-derived from the warehouse and matched against what the document says.

Three failure modes are caught:

1. MISMATCH  - the query now returns a different value than the one printed.
2. LEFTOVER  - a template token was never substituted.
3. UNBOUND   - a number in the prose that no binding accounts for.

The third is the one that matters most, because it is the failure mode of every
report generator that lets a human or a model type a number into a paragraph.

Getting it right took three bug fixes, all recorded here because the ordering is
not obvious and is easy to break again:

* Removing bound values by plain string replace corrupted text: a value "722" also
  occurs inside a snapshot hash. Values are removed only where they stand alone.
* Removing bound values before identifiers was worse: a count of "26" was cut out of
  the minute field of "15:26:16". Identifiers go first.
* Stripping identifier-like tokens in one pass was worse again: the form pattern
  matched "2B" inside "$416.2B". Hence two phases, not one list.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path

from sentinel.reports import context
from sentinel.reports.render import TOKEN_RE, apply_format

# Phase 1: things that contain digits but are identifiers, and must be removed before
# any bound value is touched, because a short value can otherwise be cut out of them.
IDENTIFIER_PATTERNS = [
    r"(?<![\d-])\d{10}-\d{2}-\d{6}(?![\d-])",                                            # accessions
    r"(?<!\d)\d{4}-\d{2}-\d{2}[T ]\d{2}:\d{2}:\d{2}(?:\.\d+)?(?:Z|[+-]\d{2}:\d{2})?(?!\d)",  # datetimes
    r"(?<!\d)\d{4}-\d{2}-\d{2}(?!\d)",                                                    # dates
    r"(?<!\d)\d{2}:\d{2}:\d{2}(?:\.\d+)?(?!\d)",                                        # times
    r"(?<![0-9a-f])[0-9a-f]{16}(?![0-9a-f])",                                                  # snapshot hashes
    r"CIK[^0-9\n]{0,8}\d+",                                                                   # CIK
    r"SIC[^0-9\n]{0,8}\d+",                                                                   # SIC
    r"Fiscal year end[^0-9\n]{0,8}\d{4}",                                                     # fiscal year-end
    r"\bv\d+\.\d+\.\d+\b",                                                               # versions
]

# Phase 2: token-like strings that are safe to strip only after bound values are gone,
# because their patterns can overlap a formatted figure ("2B" inside "$416.2B").
TOKEN_PATTERNS = [
    r"\b\d{1,3}-[A-Z]{1,3}(?:/A)?\b",          # 10-K, 8-K, 20-F
    r"\b\d{1,3}[A-Z]{1,3}\d{0,2}(?:/A)?\b",    # 13F, 424B2
    r"(?<!\d)(?:19|20)\d{2}(?!\d)",             # years, including inside FY2024
]

# A table cell of bare alphanumerics is a filing-form name (8-K, 10-Q, 144, PX14A6G).
# Money and counts always carry "$" or a thousands separator, so this cannot strip data.
FORM_CELL = r"\|\s*[A-Z0-9][A-Z0-9./-]{0,11}\s*\|"

SCAN = r"(?<![A-Za-z])\d{2,}"


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


def _apply(text: str, patterns: list[str]) -> str:
    for pattern in patterns:
        text = re.sub(pattern, " ", text)
    return text


def find_unbound(markdown: str, bindings: dict[str, dict]) -> list[str]:
    """Numbers in the prose that no binding explains.

    Three phases, in this order: identifiers, then bound values, then loose tokens.
    Each phase exists because doing it in a different order produced a wrong answer.
    """
    stripped = _apply(markdown, IDENTIFIER_PATTERNS)
    stripped = re.sub(FORM_CELL, "| |", stripped)

    for binding in sorted(bindings.values(), key=lambda b: len(str(b.get("rendered", ""))), reverse=True):
        rendered = str(binding.get("rendered", ""))
        if not rendered or rendered == "n/a":
            continue
        stripped = re.sub(r"(?<![0-9A-Za-z])" + re.escape(rendered) + r"(?![0-9A-Za-z])", " ", stripped)

    stripped = _apply(stripped, TOKEN_PATTERNS)
    stripped = re.sub(r"\|", " ", stripped)
    return sorted(set(re.findall(SCAN, stripped)))


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

    # Distinct values repeat across bindings (two years with the same margin), so the
    # occurrence test sums the uses of every binding that renders a given string.
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
            rows = con.execute(binding["sql"], binding["params"]).fetchall()
            value = rows[0][0] if rows else None
            expected = "n/a" if value is None else apply_format(binding["fmt"], float(value))
            found = str(binding.get("rendered"))
            ok = expected == found
            detail = ""
            if ok and found != "n/a":
                occurrences = len(re.findall(
                    r"(?<![0-9A-Za-z])" + re.escape(found) + r"(?![0-9A-Za-z])", markdown))
                uses = expected_occurrences.get(found, occurrences)
                # Every use must still be present. Extra matches are allowed: a count of
                # 101 can coincide with digits elsewhere. A missing match is the tamper
                # signature.
                if occurrences < uses:
                    ok = False
                    detail = "appears " + str(occurrences) + " times, expected at least " + str(uses)
            checks.append(Check(key, ok, detail, expected, found))
    finally:
        context.close_context(con)

    unbound = find_unbound(markdown, bindings)
    checks.append(Check("no_unbound_numbers", not unbound, ",".join(unbound[:10])))
    return Verification(ok=all(c.ok for c in checks), checks=checks, unbound=unbound)
