"""``robots.txt`` parsing and matching, as RFC 9309 describes it.

Each test names the rule it holds. Where the standard library or Protego answers
differently, the docstring says so, because those differences are why the
parser exists.
"""

from __future__ import annotations

from wcc.policy.robots import MAX_ROBOTS_BYTES, PRODUCT_TOKEN, parse_robots


def allows(text: str | bytes, url: str, token: str = PRODUCT_TOKEN) -> bool:
    return parse_robots(text).allows(url, token)


def test_the_longest_matching_rule_decides_whatever_its_order() -> None:
    """RFC 9309 2.2.2. Python 3.12's reader takes the first rule in file order and
    refuses ``/page`` here."""
    text = "User-agent: *\nDisallow: /\nAllow: /page\n"
    assert allows(text, "https://a.test/page")
    assert allows(text, "https://a.test/page/deeper")
    assert not allows(text, "https://a.test/other")


def test_an_allow_and_a_disallow_of_equal_length_resolve_to_allow() -> None:
    assert allows("User-agent: *\nDisallow: /page\nAllow: /page\n", "https://a.test/page")


def test_specificity_is_the_length_of_the_pattern_not_of_what_it_matched() -> None:
    """Google's library scores a rule by its pattern; Python 3.13's reader scores a
    wildcard rule by how much of the URL it covered and refuses this URL."""
    text = "User-agent: *\nAllow: /x/abcdefgh\nDisallow: /*.gif\n"
    assert allows(text, "https://a.test/x/abcdefgh.gif")
    assert not allows(text, "https://a.test/y/a.gif")


def test_a_star_matches_any_run_of_characters() -> None:
    text = "User-agent: *\nDisallow: /fish*\nDisallow: /*.php\n"
    assert not allows(text, "https://a.test/fish")
    assert not allows(text, "https://a.test/fish.html")
    assert not allows(text, "https://a.test/index.php?x=1")
    assert not allows(text, "https://a.test/a/b/c.php")
    assert allows(text, "https://a.test/Fish.asp")


def test_a_trailing_dollar_anchors_the_end_and_any_other_dollar_is_literal() -> None:
    text = "User-agent: *\nDisallow: /*.gif$\nDisallow: /a$b\n"
    assert not allows(text, "https://a.test/x/y.gif")
    assert allows(text, "https://a.test/x/y.gif?size=2")
    assert allows(text, "https://a.test/x/y.gifs")
    assert not allows(text, "https://a.test/a$b/c")
    assert allows(text, "https://a.test/ab")


def test_the_query_is_part_of_what_a_rule_is_matched_against() -> None:
    text = "User-agent: *\nDisallow: /search?q=\n"
    assert not allows(text, "https://a.test/search?q=python")
    assert allows(text, "https://a.test/search")


def test_percent_encoding_is_normalised_on_both_sides() -> None:
    """RFC 9309 2.2.2: unreserved characters compare decoded, non-ASCII compares
    as UTF-8, and an encoded reserved character stays distinct."""
    assert not allows("User-agent: *\nDisallow: /foo/%62ar\n", "https://a.test/foo/bar")
    assert not allows("User-agent: *\nDisallow: /foo/bar\n", "https://a.test/foo/%62ar")
    assert not allows("User-agent: *\nDisallow: /ツ\n", "https://a.test/%E3%83%84")
    assert not allows("User-agent: *\nDisallow: /%e3%83%84\n", "https://a.test/ツ")
    assert allows("User-agent: *\nDisallow: /a%2Fb\n", "https://a.test/a/b")


def test_the_named_group_replaces_the_star_group() -> None:
    text = "User-agent: *\nDisallow: /\n\nUser-agent: wcc\nDisallow: /private\n"
    assert allows(text, "https://a.test/page")
    assert not allows(text, "https://a.test/private")
    assert not allows(text, "https://a.test/page", token="otherbot")


def test_the_product_token_matches_without_case_or_version() -> None:
    assert not allows("User-agent: WCC\nDisallow: /\n", "https://a.test/")
    assert not allows("User-agent: wcc/0.1\nDisallow: /\n", "https://a.test/")
    assert allows("User-agent: wccbot\nDisallow: /\n", "https://a.test/")


def test_every_group_naming_the_token_is_combined() -> None:
    text = (
        "User-agent: wcc\nDisallow: /a\n\n"
        "User-agent: other\nDisallow: /\n\n"
        "User-agent: wcc\nDisallow: /b\n"
    )
    assert not allows(text, "https://a.test/a")
    assert not allows(text, "https://a.test/b")
    assert allows(text, "https://a.test/c")


def test_consecutive_user_agent_lines_share_one_group() -> None:
    text = "User-agent: other\nUser-agent: wcc\nDisallow: /x\n"
    assert not allows(text, "https://a.test/x")
    assert not allows(text, "https://a.test/x", token="other")


def test_with_no_group_for_the_token_and_no_star_group_nothing_is_restricted() -> None:
    assert allows("User-agent: otherbot\nDisallow: /\n", "https://a.test/")
    assert allows("", "https://a.test/")


def test_rules_before_any_user_agent_belong_to_no_group() -> None:
    assert allows("Disallow: /\nUser-agent: *\nAllow: /x\n", "https://a.test/page")


def test_an_empty_disallow_disallows_nothing() -> None:
    assert allows("User-agent: *\nDisallow:\n", "https://a.test/anything")


def test_robots_txt_itself_is_always_allowed() -> None:
    assert allows("User-agent: *\nDisallow: /\n", "https://a.test/robots.txt")


def test_keys_ignore_case_and_comments_and_other_records_are_skipped() -> None:
    text = (
        "# a comment\n"
        "USER-AGENT: *   # trailing comment\n"
        "Sitemap: https://a.test/sitemap.xml\n"
        "DISALLOW: /x\n"
        "Host: a.test\n"
        "disallow: /y\n"
    )
    assert not allows(text, "https://a.test/x")
    assert not allows(text, "https://a.test/y")
    assert allows(text, "https://a.test/z")


def test_a_byte_order_mark_does_not_hide_the_first_line() -> None:
    assert not allows("﻿User-agent: *\nDisallow: /\n".encode(), "https://a.test/x")


def test_only_the_first_500_kib_are_read() -> None:
    head = b"User-agent: *\nDisallow: /early\n"
    padding = b"# " + b"x" * (MAX_ROBOTS_BYTES - len(head) - 3) + b"\n"
    tail = b"Disallow: /late\n"
    parsed = parse_robots(head + padding + tail)
    assert not parsed.allows("https://a.test/early")
    assert parsed.allows("https://a.test/late")


def test_crawl_delay_is_read_for_the_group_that_applies() -> None:
    text = "User-agent: *\nCrawl-delay: 10\n\nUser-agent: wcc\nCrawl-delay: 2.5\nDisallow: /x\n"
    parsed = parse_robots(text)
    assert parsed.crawl_delay() == 2.5
    assert parsed.crawl_delay("otherbot") == 10
    assert not parsed.allows("https://a.test/x")


def test_a_crawl_delay_that_is_not_a_number_is_ignored() -> None:
    for value in ("soon", "-1", "nan", "inf"):
        assert parse_robots(f"User-agent: *\nCrawl-delay: {value}\n").crawl_delay() is None


def test_a_hostile_pattern_is_matched_in_linear_time() -> None:
    """A regex built from ``/*a*a*a*...`` backtracks exponentially on a long miss."""
    text = "User-agent: *\nDisallow: /" + "*a" * 200 + "b\n"
    assert allows(text, "https://a.test/" + "a" * 5_000)
