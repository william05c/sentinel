"""robots.txt: fetch, parse, cache, obey.

Implements RFC 9309. The rules that matter and are easy to get wrong:

* A 2xx response means obey the rules. A 4xx means the file is "unavailable" and the
  crawler MAY access anything. A 5xx or a network failure means the file is
  "unreachable" and the crawler MUST assume complete disallow.
* Matching is longest-match on the path, with * as a wildcard and $ anchoring the end.
  Ties go to the longer match; a tie on length goes to Allow.
* Cached copies SHOULD NOT be used for more than 24 hours, and the parser must accept
  at least 500 KiB.

crawl-delay is not part of RFC 9309. It is honoured when present because sites that
publish it are telling us something, but the rate limiter remains the authority.
"""

from __future__ import annotations

import re
import time
from dataclasses import dataclass, field
from urllib.parse import urlparse

MAX_BYTES = 512 * 1024          # RFC 9309 2.5: parse limit MUST be >= 500 KiB
CACHE_TTL_S = 24 * 3600         # RFC 9309 2.4: SHOULD NOT reuse beyond 24 hours


@dataclass
class Rule:
    pattern: str
    allow: bool
    regex: re.Pattern

    @classmethod
    def build(cls, pattern: str, allow: bool) -> "Rule":
        anchored = pattern.endswith("$")
        body = pattern[:-1] if anchored else pattern
        expr = "".join(".*" if ch == "*" else re.escape(ch) for ch in body)
        return cls(pattern=pattern, allow=allow, regex=re.compile("^" + expr + ("$" if anchored else "")))


@dataclass
class RobotsPolicy:
    """The rules that apply to one user agent on one host."""

    rules: list[Rule] = field(default_factory=list)
    crawl_delay: float | None = None
    allow_all: bool = False
    disallow_all: bool = False
    fetched_at: float = 0.0

    def allowed(self, path: str) -> bool:
        if self.disallow_all:
            return False
        if self.allow_all:
            return True
        if not path:
            path = "/"
        best_len = -1
        best_allow = True
        for rule in self.rules:
            if rule.regex.match(path):
                # longest match wins; on a tie Allow beats Disallow
                if len(rule.pattern) > best_len or (len(rule.pattern) == best_len and rule.allow):
                    best_len = len(rule.pattern)
                    best_allow = rule.allow
        return best_allow if best_len >= 0 else True

    def stale(self, now: float | None = None) -> bool:
        return ((now or time.time()) - self.fetched_at) > CACHE_TTL_S


def _agent_score(group_agents: list[str], product: str) -> int:
    """How specifically this group names us. -1 means it does not apply at all.

    A group naming our product beats the wildcard group; it does not merge with it.
    Merging would let a site's catch-all rules override rules written for us.
    """
    product = product.lower()
    best = -1
    for agent in group_agents:
        token = agent.strip().lower()
        if token == "*":
            best = max(best, 0)
        elif token and token in product:
            best = max(best, len(token))
    return best


def parse(text: str, product: str) -> RobotsPolicy:
    """Parse robots.txt for one product token (e.g. "sentinel-research")."""
    if len(text) > MAX_BYTES:
        text = text[:MAX_BYTES]

    policy = RobotsPolicy(fetched_at=time.time())
    groups: list[tuple[list[str], list[Rule], float | None]] = []
    agents: list[str] = []
    rules: list[Rule] = []
    delay: float | None = None
    seen_rule = False

    for raw in text.splitlines():
        line = raw.split("#", 1)[0].strip()
        if not line or ":" not in line:
            continue
        field_name, _, value = line.partition(":")
        field_name = field_name.strip().lower()
        value = value.strip()

        if field_name == "user-agent":
            if seen_rule:
                groups.append((agents, rules, delay))
                agents, rules, delay, seen_rule = [], [], None, False
            agents.append(value)
        elif field_name in ("allow", "disallow"):
            seen_rule = True
            if field_name == "disallow" and value == "":
                continue  # an empty Disallow means "allow everything"; it is not a rule
            rules.append(Rule.build(value, field_name == "allow"))
        elif field_name == "crawl-delay":
            seen_rule = True
            try:
                delay = max(0.0, float(value))
            except ValueError:
                pass
    if agents or rules:
        groups.append((agents, rules, delay))

    # Only the most specific matching group applies (RFC 9309 2.2.1).
    best_score = -1
    merged: list[Rule] = []
    for group_agents, group_rules, group_delay in groups:
        score = _agent_score(group_agents, product)
        if score < 0:
            continue
        if score > best_score:
            best_score, merged = score, list(group_rules)
            policy.crawl_delay = group_delay
        elif score == best_score:
            merged.extend(group_rules)
            if policy.crawl_delay is None:
                policy.crawl_delay = group_delay
    policy.rules = merged
    if not merged:
        policy.allow_all = True
    return policy


def for_status(status: int, text: str | None, product: str) -> RobotsPolicy:
    """Build the policy implied by an HTTP response to /robots.txt."""
    from sentinel.crawler.net import utcnow  # local import keeps the module importable alone

    if 200 <= status < 300:
        return parse(text or "", product)
    if 400 <= status < 500:
        # "unavailable": the owner has not published rules, so nothing is forbidden
        return RobotsPolicy(allow_all=True, fetched_at=time.time())
    # 5xx or anything else: RFC 9309 says assume complete disallow
    return RobotsPolicy(disallow_all=True, fetched_at=time.time())
