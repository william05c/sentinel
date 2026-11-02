"""Extract sections from a filing document.

A 10-K is 100+ pages of boilerplate wrapped around six or seven sections that matter.
Extracting by Item heading - rather than reading the whole document - is the discipline
borrowed from the EDGAR-CORPUS work, reimplemented here because that project is GPL and
this repository is MIT.

Everything is a pure function over bytes, so it is testable without a network.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

from lxml import html as lxml_html

ITEM_NAMES_10K = {
    "1": "Business",
    "1A": "Risk Factors",
    "1B": "Unresolved Staff Comments",
    "2": "Properties",
    "3": "Legal Proceedings",
    "4": "Mine Safety Disclosures",
    "5": "Market for Registrant's Common Equity",
    "6": "Selected Financial Data",
    "7": "Management's Discussion and Analysis",
    "7A": "Quantitative and Qualitative Disclosures About Market Risk",
    "8": "Financial Statements and Supplementary Data",
    "9": "Changes in and Disagreements with Accountants",
    "9A": "Controls and Procedures",
    "9B": "Other Information",
    "10": "Directors, Executive Officers and Corporate Governance",
    "11": "Executive Compensation",
    "12": "Security Ownership of Certain Beneficial Owners",
    "13": "Certain Relationships and Related Transactions",
    "14": "Principal Accountant Fees and Services",
    "15": "Exhibits and Financial Statement Schedules",
}

HEADING = re.compile(r"\bItem\s+(\d{1,2}[A-C]?)\s*[.:\u2014-]\s*([A-Z][^\n]{0,90})", re.IGNORECASE)
MIN_SECTION_CHARS = 400


@dataclass
class Section:
    section_id: str
    name: str
    text: str

    @property
    def char_count(self) -> int:
        return len(self.text)


def to_text(raw: bytes) -> str:
    """HTML to readable text. Tables become rows, not a run-together paragraph."""
    try:
        doc = lxml_html.fromstring(raw)
    except Exception:
        return ""
    for bad in doc.xpath("//script|//style"):
        bad.drop_tree()
    blocks = []
    for node in doc.iter("p", "div", "td", "li"):
        text = " ".join(node.text_content().split())
        if len(text) > 40:
            blocks.append(text)
        elif 8 < len(text) <= 40 and HEADING.search(text):
            # A section heading is short by nature. Dropping short blocks dropped
            # "Item 1A. Risk Factors" along with the furniture, which silently removed
            # the most useful section in the document.
            blocks.append(text)
    joined = "\n".join(blocks)
    return re.sub(r"\n{3,}", "\n\n", joined)


def extract_sections(text: str, item_names: dict[str, str] | None = None) -> list[Section]:
    """Split on Item headings and keep the sections that are long enough to mean anything.

    A heading is only trusted when it is followed by a plausible title or sits at the
    start of a line; a table of contents is full of "Item 7." and would otherwise
    produce twenty empty sections.
    """
    names = item_names or ITEM_NAMES_10K
    marks: list[tuple[int, str]] = []
    for match in HEADING.finditer(text):
        item = match.group(1).upper()
        if item not in names:
            continue
        marks.append((match.start(), item))

    sections: list[Section] = []
    for index, (position, item) in enumerate(marks):
        end = marks[index + 1][0] if index + 1 < len(marks) else len(text)
        body = text[position:end].strip()
        if len(body) < MIN_SECTION_CHARS:
            continue
        if sections and sections[-1].section_id == item:
            continue  # a repeated heading in running text, not a new section
        sections.append(Section(section_id=item, name=names[item], text=body))
    return sections


def summarise(sections: list[Section]) -> dict[str, int]:
    return {s.section_id: s.char_count for s in sections}
