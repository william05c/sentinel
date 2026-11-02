"""Text measures: tone, readability and what changed since last year.

The sentiment dictionaries here are a compact subset written for this project in the
spirit of Loughran-McDonald - the full list is licensed and was not copied. That makes
the scores comparable WITHIN this warehouse over time, and not comparable with published
LM scores. Stated here because it would otherwise be assumed.
"""

from __future__ import annotations

import math
import re
from collections import Counter
from dataclasses import dataclass

NEGATIVE = set("""abandoned adverse adversely alleging ambiguity breach breaches claim claims
claims declined default deficiency deficient delay delays deteriorate deteriorated
deterioration difficult difficulty disclosed dispute disputed doubt doubts downturn
failure failed failing fraud fraudulent impairment impair impaired inadequate
investigation investigations jeopardize lawsuit lawsuits litigation loss losses
misstatement misstatement negative penalties penalty poor prohibit questionable
reduction reductions restatement restated risk risks significant significantly
slowdown terminate terminated termination unable uncertainty unfavorable violation
violations volatility weak weaken worsened worsening write-off write-down""".split())

POSITIVE = set("""achieve achieved achieving advantage attractive benefit beneficial
confidence confident deliver delivered delivering efficient enhance enhanced enhancing
excellent favorable gain gains grew grow growth improve improved improvement improving
increase increased increasing leadership opportunity opportunities optimise optimized
outperform profitable profit profitability progress reliable strength strengthen
strong success successful successfully sustainable""".split())

UNCERTAINTY = set("""approximately assume assumed assumption believe believes could
depend depends estimate estimated estimates expect expected expecting fluctuate
fluctuations may might possible possibly potential potentially predict projected
seek should uncertain uncertainty unclear unknown vary varies various""".split())

LITIGIOUS = set("""allegation allegations alleged claim plaintiff plaintiffs complaint
complaints court damages defendant defendants judgment lawsuit litigation proceedings
settlement""".split())

CONSTRAINING = set("""covenant covenants default defaulted encumbered guarantee
guarantees lien liens obligation obligations pledge pledged prohibit prohibited
restrict restricted restriction restrictions""".split())

WORD = re.compile(r"[A-Za-z][A-Za-z'\-]+")
SENTENCE = re.compile(r"[^.!?]+[.!?]")


def words(text: str) -> list[str]:
    return WORD.findall(text.lower())


def _syllables(word: str) -> int:
    word = word.lower()
    groups = re.findall(r"[aeiouy]+", word)
    count = len(groups)
    if word.endswith("e") and count > 1:
        count -= 1
    return max(1, count)


def fog_index(text: str) -> float:
    """Gunning Fog. Higher means harder to read; 18 is roughly a legal contract."""
    sentences = SENTENCE.findall(text) or [text]
    ws = words(text)
    if not ws:
        return 0.0
    complex_words = sum(1 for w in ws if _syllables(w) >= 3)
    return round(0.4 * ((len(ws) / max(1, len(sentences))) + 100 * complex_words / len(ws)), 3)


@dataclass
class Tone:
    negative: float
    positive: float
    uncertainty: float
    litigious: float
    constraining: float
    words: int
    fog: float

    def as_dict(self) -> dict:
        return {
            "sentiment_neg": self.negative, "sentiment_pos": self.positive,
            "sentiment_unc": self.uncertainty, "litigious": self.litigious,
            "constraining": self.constraining, "words": self.words, "fog_index": self.fog,
        }


def tone(text: str) -> Tone:
    """Category counts per 1,000 words, plus readability."""
    ws = words(text)
    if not ws:
        return Tone(0, 0, 0, 0, 0, 0, 0.0)
    total = len(ws)
    per_k = lambda bucket: round(sum(1 for w in ws if w in bucket) / total * 1000, 3)
    return Tone(per_k(NEGATIVE), per_k(POSITIVE), per_k(UNCERTAINTY),
                per_k(LITIGIOUS), per_k(CONSTRAINING), total, fog_index(text))


PHRASE = re.compile(r"[a-z][a-z'\-]+(?:\s+[a-z][a-z'\-]+){1,3}")

# Phrases built only from these carry no signal about what changed, but they rank at the
# top of every drift list because they are the most common n-grams in English.
FILLER = set("""a an and are as at be been but by due for from has have if in into is it
its may not of on or other our that the their them then there these they this those to
was we were which will with would primarily partially offset compared""".split())


def phrases(text: str, n: int = 2) -> Counter:
    ws = words(text)
    grams = [" ".join(ws[i:i + n]) for i in range(len(ws) - n + 1)]
    return Counter(grams)


def _tidy(phrase: str) -> str:
    """Drop leading and trailing filler so a phrase reads as a topic, not a sentence."""
    parts = phrase.split()
    while parts and (parts[0] in FILLER or parts[0] in ("a", "an", "the")):
        parts.pop(0)
    while parts and (parts[-1] in FILLER or parts[-1] in ("a", "an", "the")):
        parts.pop()
    return " ".join(parts)


def narrative_drift(current: str, prior: str, top: int = 10, n: int = 3) -> list[dict]:
    """Phrases that became prominent this year but were not last year.

    Scored by the rise in rate per 1,000 words rather than raw counts, so a longer
    document does not look like it drifted simply by being longer.
    """
    cur_words = max(1, len(words(current)))
    pri_words = max(1, len(words(prior)))
    cur, pri = phrases(current, n), phrases(prior, n)

    scored = []
    for phrase, count in cur.items():
        if count < 3:
            continue
        content = [w for w in phrase.split() if w not in FILLER and len(w) > 3]
        if not content:
            continue  # "due to" is not a finding
        rate_now = count / cur_words * 1000
        rate_before = pri.get(phrase, 0) / pri_words * 1000
        rise = rate_now - rate_before
        if rise > 0.15:
            scored.append({"phrase": phrase, "now": round(rate_now, 3),
                           "before": round(rate_before, 3), "rise": round(rise, 3)})
    scored.sort(key=lambda d: d["rise"], reverse=True)

    # Drop phrases that are contained in a stronger one: "stock price" and "our stock
    # price" are the same finding and should appear once.
    def content_of(phrase: str) -> set[str]:
        return {w for w in phrase.split() if w not in FILLER and len(w) > 3}

    kept: list[dict] = []
    for candidate in scored:
        if any(candidate["phrase"] in other["phrase"] for other in kept):
            continue
        cand_words = content_of(candidate["phrase"])
        # Sliding trigrams of one sentence ("financial condition and", "of operations
        # financial") are the same finding four times. Two shared content words is
        # enough to call them the same.
        if any(cand_words & content_of(other["phrase"]) for other in kept):
            continue
        candidate["phrase"] = _tidy(candidate["phrase"])
        if not candidate["phrase"]:
            continue
        kept.append(candidate)
        if len(kept) >= top:
            break
    return kept


def boilerplate_ratio(current: str, prior: str, n: int = 5) -> float:
    """Share of this year's phrases that are copied verbatim from last year."""
    cur, pri = phrases(current, n), phrases(prior, n)
    if not cur:
        return 0.0
    shared = sum(min(count, pri.get(phrase, 0)) for phrase, count in cur.items())
    return round(shared / sum(cur.values()), 3)
