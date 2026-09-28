import pytest
from contracts.web_agent import HIDDEN_UNICODE_FLAG, INJECTION_FLAGS
from pydantic import ValidationError
from web_agent.formatting import format_text
from web_agent.guard.injection import PATTERNS
from web_agent.models import SearchQuery, SearchResponse, SearchResult


def test_search_query_defaults_and_strips() -> None:
    query = SearchQuery(query="  x  ")

    assert (query.query, query.max_results, query.region) == ("x", 5, None)


@pytest.mark.parametrize(
    "payload",
    [
        {},
        {"query": ""},
        {"query": "   "},
        {"query": "x" * 401},
        {"query": "x", "max_results": 0},
        {"query": "x", "max_results": 11},
        {"query": "x", "region": "Poland"},
    ],
)
def test_search_query_rejects_invalid_input(payload: dict) -> None:
    with pytest.raises(ValidationError):
        SearchQuery.model_validate(payload)


def _response(status: str, **kwargs) -> SearchResponse:
    return SearchResponse(status=status, engine="duckduckgo", query="q", elapsed_ms=1, **kwargs)


def test_format_text_lists_each_url_once() -> None:
    results = [
        SearchResult(rank=i, title=f"T{i}", url=f"https://example.com/{i}", snippet="s", domain="example.com")
        for i in (1, 2)
    ]

    text = format_text(_response("ok", results=results))

    assert text.count("https://example.com/1") == 1
    assert text.count("https://example.com/2") == 1


def test_format_text_blocked_is_one_sentence_with_reason() -> None:
    text = format_text(_response("blocked", error="DuckDuckGo answered HTTP 429"))

    assert "HTTP 429" in text
    assert "\n" not in text


def test_injection_patterns_match_the_contract_flags() -> None:
    # api names a withheld result's reasons from `INJECTION_FLAGS`; each must be a real signal.
    assert set(PATTERNS) | {HIDDEN_UNICODE_FLAG} == INJECTION_FLAGS
