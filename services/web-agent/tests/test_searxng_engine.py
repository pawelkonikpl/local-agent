from urllib.parse import parse_qs, urlparse

import pytest

from web_agent.engines import get_engine
from web_agent.engines.searxng import SearxngEngine, language_for
from web_agent.models import SearchQuery


@pytest.mark.parametrize(
    ("region", "language"), [("pl-pl", "pl-PL"), ("us-en", "en-US"), ("de-de", "de-DE"), ("wt-wt", "all")]
)
def test_language_for(region: str, language: str) -> None:
    assert language_for(region) == language


def test_url_for() -> None:
    url = SearxngEngine("http://searxng:8080/").url_for(SearchQuery(query="a&b c"), "pl-pl")

    parsed = urlparse(url)
    assert f"{parsed.scheme}://{parsed.netloc}{parsed.path}" == "http://searxng:8080/search"
    assert parse_qs(parsed.query) == {"q": ["a&b c"], "language": ["pl-PL"], "categories": ["general"]}


def test_parse_keeps_direct_links_and_drops_the_rest() -> None:
    raw = [
        {"title": "A", "href": "https://example.com/a", "snippet": "s", "is_ad": False},
        {"title": "Relative", "href": "/stats?engine=brave", "snippet": "", "is_ad": False},
        {"title": "A again", "href": "https://example.com/a#x", "snippet": "", "is_ad": False},
    ]

    results = SearxngEngine("http://searxng:8080").parse(raw, max_results=5)

    assert [r.url for r in results] == ["https://example.com/a"]


def test_detect_block_and_no_results() -> None:
    engine = SearxngEngine("http://searxng:8080")

    assert engine.detect_block(429, "")
    assert engine.detect_block(200, "Sorry! No results were found.") is None
    assert engine.is_no_results("Sorry!\nNo results were found. You can try to:")


def test_get_engine_requires_url_for_searxng() -> None:
    with pytest.raises(ValueError, match="SEARXNG_URL"):
        get_engine("searxng")
    assert get_engine("searxng", searxng_url="http://searxng:8080").name == "searxng"
