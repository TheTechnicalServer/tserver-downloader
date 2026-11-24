"""Tests for the ad-block / privacy engine (Phase 4)."""
import pytest

from tserver_downloader.adblock import AdBlockEngine, compile_rules
from tserver_downloader.adblock.engine import DEFAULT_ALLOWLIST
from tserver_downloader.adblock.filters import parse_filter_line
from tserver_downloader.adblock.lists import (
    DEFAULT_LISTS,
    fetch_list,
    load_bundled_lists,
)


def test_parse_domain_anchor():
    rule = parse_filter_line("||doubleclick.net^")
    assert rule is not None
    assert rule.is_domain_anchor
    assert rule.domain_anchor == "doubleclick.net"
    assert not rule.is_exception
    assert not rule.is_cosmetic


def test_parse_exception():
    rule = parse_filter_line("@@||example.com^$document")
    assert rule is not None
    assert rule.is_exception
    assert rule.domain_anchor == "example.com"


def test_parse_cosmetic():
    rule = parse_filter_line("example.com##.ad-banner")
    assert rule is not None
    assert rule.is_cosmetic
    assert rule.selector == ".ad-banner"
    assert rule.domains == ["example.com"]


def test_parse_cosmetic_exception():
    rule = parse_filter_line("example.com#@#.ad-banner")
    assert rule is not None
    assert rule.is_cosmetic and rule.is_exception
    assert rule.selector == ".ad-banner"


def test_parse_generic_cosmetic():
    rule = parse_filter_line("##.sponsored")
    assert rule is not None and rule.is_cosmetic
    assert rule.domains == []


def test_parse_domain_option():
    rule = parse_filter_line("||ads.example.com^$domain=site.com|other.org")
    assert rule is not None
    assert rule.domains == ["site.com", "other.org"]


def test_parse_resource_type_options():
    rule = parse_filter_line("||ads.example.com^$script,image")
    assert rule is not None
    assert rule.resource_types == {"script", "image"}


def test_parse_third_party_option():
    rule = parse_filter_line("||ads.example.com^$third-party")
    assert rule is not None and rule.third_party is True


def test_parse_comments_ignored():
    assert parse_filter_line("! a comment") is None
    assert parse_filter_line("[Adblock Plus 2.0]") is None
    assert parse_filter_line("") is None
    assert parse_filter_line("   ") is None


def test_parse_wildcard_and_prefix():
    rule = parse_filter_line("|http://ads.example.com/banner*")
    assert rule is not None
    assert rule.matches("http://ads.example.com/banner123.gif")
    assert not rule.matches("https://ads.example.com/banner123.gif")


def _engine_with_bundled():
    engine = AdBlockEngine()
    engine.load_lists(["\n".join(load_bundled_lists())])
    return engine


def test_should_block_ad_domain():
    engine = _engine_with_bundled()
    assert engine.should_block("https://doubleclick.net/ads/foo")
    assert engine.should_block("https://www.googlesyndication.com/pagead/js/adsbygoogle.js")
    assert engine.should_block("https://ads.yahoo.com/pixel", resource_type="image")


def test_should_not_block_clean_site():
    engine = _engine_with_bundled()
    assert not engine.should_block("https://example.org/article")
    assert not engine.should_block("https://en.wikipedia.org/wiki/Ad_blocking")


def test_exception_overrides_block():
    engine = AdBlockEngine()
    engine.load_lists(["||example.com^\n@@||example.com^"])
    assert not engine.should_block("https://example.com/page")


def test_exception_with_domain_option():
    engine = AdBlockEngine()
    engine.load_lists(["||ads.example.com^\n@@||ads.example.com^$domain=trusted.com"])
    assert not engine.should_block("https://ads.example.com/x", page_domain="trusted.com")
    assert engine.should_block("https://ads.example.com/x", page_domain="other.com")


def test_allowlist_blog_never_blocked():
    engine = _engine_with_bundled()
    # Even an ad-like URL on the blog host must never be blocked.
    assert "thetechnicalserver.blogspot.com" in DEFAULT_ALLOWLIST
    assert not engine.should_block("https://thetechnicalserver.blogspot.com/ads/banner.js")
    assert not engine.should_block(
        "https://thetechnicalserver.blogspot.com/doubleclick.net/tracker")
    assert not engine.should_block(
        "https://www.thetechnicalserver.blogspot.com/pagead/show_ads.js")


def test_allowlist_parameter():
    engine = AdBlockEngine(allowlist=["mysite.example"])
    engine.load_lists(["||mysite.example^"])
    assert not engine.should_block("https://mysite.example/page")


def test_cosmetic_selectors():
    engine = AdBlockEngine()
    engine.load_lists(["example.com##.ad-banner\n##.sponsored\n"])
    selectors = engine.get_cosmetic_selectors("example.com")
    assert ".ad-banner" in selectors
    assert ".sponsored" in selectors
    other = engine.get_cosmetic_selectors("other.com")
    assert ".sponsored" in other
    assert ".ad-banner" not in other


def test_cosmetic_exception_removes_selector():
    engine = AdBlockEngine()
    engine.load_lists(["##.ad-banner\nexample.com#@#.ad-banner\n"])
    assert ".ad-banner" not in engine.get_cosmetic_selectors("example.com")
    assert ".ad-banner" in engine.get_cosmetic_selectors("other.com")


def test_stats_counting():
    engine = _engine_with_bundled()
    engine.should_block("https://doubleclick.net/a")
    engine.should_block("https://doubleclick.net/b")
    engine.should_block("https://example.org/clean")
    stats = engine.stats
    assert stats["blocked_count"] == 2
    assert stats["allowed_count"] == 1
    per_domain = stats["per-domain counts"]
    assert per_domain.get("doubleclick.net") == 2


def test_bundled_lists_load():
    rules = load_bundled_lists()
    assert len(rules) >= 20
    text = "\n".join(rules)
    assert "||doubleclick.net^" in text
    assert "||googlesyndication.com^" in text
    engine = AdBlockEngine()
    engine.load_lists([text])
    assert engine.rule_count() >= 20


def test_default_lists():
    assert len(DEFAULT_LISTS) >= 2
    urls = [entry["url"] for entry in DEFAULT_LISTS]
    assert all(url.startswith("https://") for url in urls)
    assert any("easylist" in url for url in urls)


def test_fetch_list_rejects_http():
    with pytest.raises(ValueError):
        fetch_list("http://example.com/easylist.txt")


def test_update_lists_with_fake_fetch():
    engine = AdBlockEngine()
    calls = []

    def fake_fetch(url):
        calls.append(url)
        assert url.startswith("https://")
        return "||fakeads.example^\n"

    loaded = engine.update_lists(fetch_fn=fake_fetch)
    assert loaded == len(DEFAULT_LISTS)
    assert calls
    assert engine.should_block("https://fakeads.example/x")


def test_update_lists_skips_empty_and_bad_fetch():
    engine = AdBlockEngine()

    def fake_fetch(url):
        if "easyprivacy" in url:
            return "   "
        return "||fakeads.example^\n"

    loaded = engine.update_lists(fetch_fn=fake_fetch)
    assert loaded == 1
    assert engine.should_block("https://fakeads.example/x")


def test_add_custom_rule():
    engine = AdBlockEngine()
    rule = engine.add_custom_rule("||customads.example^")
    assert rule is not None
    assert engine.should_block("https://customads.example/banner")
    assert engine.add_custom_rule("! comment") is None


def test_compile_rules():
    rules = compile_rules("||a.example^\n! comment\n@@||b.example^\n")
    assert len(rules) == 2


def test_resource_type_filtering():
    engine = AdBlockEngine()
    engine.load_lists(["||ads.example.com^$script"])
    assert engine.should_block("https://ads.example.com/x.js", resource_type="script")
    assert not engine.should_block("https://ads.example.com/x.png", resource_type="image")
