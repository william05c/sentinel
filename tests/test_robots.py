"""robots.txt: the RFC 9309 rules that are easy to get wrong."""

from __future__ import annotations

from sentinel.crawler import robots

UA = "sentinel-research"


def test_simple_disallow_and_default_allow():
    policy = robots.parse("User-agent: *\nDisallow: /private\n", UA)
    assert not policy.allowed("/private")
    assert not policy.allowed("/private/data")
    assert policy.allowed("/public")


def test_longest_match_wins_and_allow_beats_disallow_on_tie():
    text = "User-agent: *\nDisallow: /a\nAllow: /a/b\n"
    policy = robots.parse(text, UA)
    assert not policy.allowed("/a/x")
    assert policy.allowed("/a/b/c")

    tie = robots.parse("User-agent: *\nDisallow: /x\nAllow: /x\n", UA)
    assert tie.allowed("/x"), "on an equal-length match, Allow wins"


def test_wildcards_and_end_anchor():
    policy = robots.parse("User-agent: *\nDisallow: /*.pdf$\n", UA)
    assert not policy.allowed("/docs/report.pdf")
    assert policy.allowed("/docs/report.pdf.html")
    assert policy.allowed("/docs/report.htm")


def test_specific_agent_beats_star():
    text = (
        "User-agent: *\nDisallow: /\n\n"
        "User-agent: sentinel-research\nDisallow: /secret\n"
    )
    policy = robots.parse(text, UA)
    assert policy.allowed("/anything")
    assert not policy.allowed("/secret/x")


def test_empty_disallow_allows_everything():
    policy = robots.parse("User-agent: *\nDisallow:\n", UA)
    assert policy.allowed("/whatever")


def test_crawl_delay_is_read_when_published():
    policy = robots.parse("User-agent: *\nCrawl-delay: 2.5\nDisallow: /x\n", UA)
    assert policy.crawl_delay == 2.5


def test_status_semantics():
    """4xx means no rules published; 5xx means assume complete disallow."""
    assert robots.for_status(404, None, UA).allow_all is True
    assert robots.for_status(200, "User-agent: *\nDisallow: /x\n", UA).allowed("/x") is False
    unreachable = robots.for_status(503, None, UA)
    assert unreachable.disallow_all is True
    assert unreachable.allowed("/") is False


def test_cache_expires_after_24h():
    policy = robots.parse("User-agent: *\nDisallow: /x\n", UA)
    assert policy.stale(policy.fetched_at + 60) is False
    assert policy.stale(policy.fetched_at + 25 * 3600) is True
