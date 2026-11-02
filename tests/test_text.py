"""Text measures: tone, readability and drift, on text whose answer is known."""

from __future__ import annotations

from sentinel.adapters import filing_text
from sentinel.analytics import text as text_mod

RISK = "We face significant risks. Litigation and investigations may result in losses and penalties. " * 5
GOOD = "We delivered strong growth and improved profitability. Our success reflects confident execution. " * 5


def test_tone_separates_negative_from_positive():
    bad = text_mod.tone(RISK)
    good = text_mod.tone(GOOD)
    assert bad.negative > good.negative
    assert good.positive > bad.positive
    assert bad.litigious > 0


def test_tone_is_per_thousand_words():
    short = text_mod.tone("risk")
    long = text_mod.tone("risk " + "the " * 199)   # one negative word in two hundred
    assert short.words == 1 and long.words == 200
    assert abs(short.negative - 1000.0) < 1e-6     # 1 in 1
    assert abs(long.negative - 5.0) < 1e-6         # 1 in 200


def test_fog_rises_with_complexity():
    simple = text_mod.fog_index("The cat sat on the mat. The dog ran fast.")
    dense = text_mod.fog_index(
        "Notwithstanding the foregoing, the aforementioned indemnification obligations "
        "shall be construed as cumulative and non-exclusive."
    )
    assert dense > simple


def test_drift_finds_new_language_and_ignores_filler():
    prior = "Our business depends on supply chain partners. " * 20
    current = prior + ("We are exposed to tariff and trade restriction exposure from new policy. " * 6)
    rows = text_mod.narrative_drift(current, prior)
    phrases = [r["phrase"] for r in rows]
    assert phrases, "a genuinely new theme must register"
    assert any("tariff" in p for p in phrases)
    assert not any(p.strip() in ("due to", "and other") for p in phrases)


def test_drift_reports_nothing_for_identical_text():
    same = RISK
    assert text_mod.narrative_drift(same, same) == []


def test_boilerplate_ratio_extremes():
    same = "the quick brown fox jumps over the lazy dog " * 30
    assert text_mod.boilerplate_ratio(same, same) > 0.9
    other = "completely different language about tariffs and semiconductor supply " * 30
    assert text_mod.boilerplate_ratio(other, same) < 0.1


def test_sections_extract_by_item_heading():
    html = (
        "<html><body>"
        "<div>Item 1. Business</div>"
        "<p>" + "We design and sell consumer devices worldwide. " * 20 + "</p>"
        "<div>Item 1A. Risk Factors</div>"
        "<p>" + "We face significant risks from competition and litigation. " * 20 + "</p>"
        "<div>Item 7. Management's Discussion and Analysis</div>"
        "<p>" + "Revenue increased and margins expanded during the year. " * 20 + "</p>"
        "</body></html>"
    ).encode()
    sections = filing_text.extract_sections(filing_text.to_text(html))
    ids = [s.section_id for s in sections]
    assert ids == ["1", "1A", "7"]
    assert sections[1].name == "Risk Factors"
    assert "litigation" in sections[1].text.lower()


def test_short_heading_is_not_dropped():
    """The 40-character filter once deleted 'Item 1A. Risk Factors' and with it the section."""
    assert filing_text.HEADING.search("Item 1A. Risk Factors") is not None
