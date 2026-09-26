from pathlib import Path
from urllib.parse import parse_qs, urlparse

import pytest

from web_agent.engines import get_engine
from web_agent.engines.duckduckgo import DuckDuckGoEngine
from web_agent.models import SearchQuery

FIXTURES = Path(__file__).parent / "fixtures"


def _entry(href: str, title: str = "Title", snippet: str = "Snippet", is_ad: bool = False) -> dict:
    return {"title": title, "href": href, "snippet": snippet, "is_ad": is_ad}


@pytest.fixture
def engine() -> DuckDuckGoEngine:
    return DuckDuckGoEngine()


def test_parse_decodes_redirect_links(engine: DuckDuckGoEngine) -> None:
    raw = [_entry("//duckduckgo.com/l/?uddg=https%3A%2F%2Fexample.com%2Fa%3Fb%3D1&rut=abc")]

    [result] = engine.parse(raw, max_results=5)

    assert result.url == "https://example.com/a?b=1"
    assert result.domain == "example.com"
    assert result.rank == 1


def test_parse_strips_www_from_domain(engine: DuckDuckGoEngine) -> None:
    [result] = engine.parse([_entry("https://www.python.org/downloads/")], max_results=5)

    assert result.domain == "python.org"


def test_parse_drops_ads(engine: DuckDuckGoEngine) -> None:
    raw = [_entry("https://ad.example.com", is_ad=True), _entry("https://example.com")]

    assert [r.url for r in engine.parse(raw, max_results=5)] == ["https://example.com"]


@pytest.mark.parametrize(
    "href",
    [
        "javascript:alert(1)",
        "mailto:someone@example.com",
        "//duckduckgo.com/y.js?ad_provider=bing",
        "//duckduckgo.com/l/?rut=no-target",
        "",
    ],
)
def test_parse_drops_non_http_and_ddg_internal_links(engine: DuckDuckGoEngine, href: str) -> None:
    assert engine.parse([_entry(href)], max_results=5) == []


def test_parse_deduplicates_urls_differing_only_by_fragment(engine: DuckDuckGoEngine) -> None:
    raw = [_entry("https://example.com/page#one"), _entry("https://example.com/page#two")]

    assert len(engine.parse(raw, max_results=5)) == 1


def test_parse_caps_results_and_numbers_ranks(engine: DuckDuckGoEngine) -> None:
    raw = [_entry(f"https://example.com/{i}") for i in range(8)]

    results = engine.parse(raw, max_results=5)

    assert [r.rank for r in results] == [1, 2, 3, 4, 5]


def test_parse_normalizes_whitespace_and_truncates_snippet(engine: DuckDuckGoEngine) -> None:
    raw = [_entry("https://example.com", title="  Two\n  lines ", snippet="word " * 200)]

    [result] = engine.parse(raw, max_results=5)

    assert result.title == "Two lines"
    assert len(result.snippet) == 300


def test_url_for_encodes_query_and_region(engine: DuckDuckGoEngine) -> None:
    url = engine.url_for(SearchQuery(query="a&b c"), "pl-pl")

    assert parse_qs(urlparse(url).query) == {"q": ["a&b c"], "kl": ["pl-pl"]}


def test_detect_block_on_rate_limit_status(engine: DuckDuckGoEngine) -> None:
    assert engine.detect_block(429, "")


def test_detect_block_on_bot_challenge_page(engine: DuckDuckGoEngine) -> None:
    text = "Unfortunately, bots use DuckDuckGo too. Please complete the following challenge"

    assert engine.detect_block(202, text)


def test_detect_block_passes_normal_results_page(engine: DuckDuckGoEngine) -> None:
    assert engine.detect_block(200, "What's New In Python 3.13 docs.python.org") is None


def test_is_no_results(engine: DuckDuckGoEngine) -> None:
    assert engine.is_no_results("DuckDuckGo\nNo results.\nFeedback")
    assert not engine.is_no_results("1. What's New In Python 3.13")


def test_get_engine_rejects_unknown_name() -> None:
    with pytest.raises(ValueError, match="Unknown search engine"):
        get_engine("altavista")
