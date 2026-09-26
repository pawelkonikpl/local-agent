import json

import httpx
import pytest

from api.tools.base import ToolInputError
from api.tools.registry import ToolRegistry
from api.tools.web_search import WebSearchTool

OK_BODY = {
    "status": "ok",
    "engine": "duckduckgo",
    "query": "python 3.13",
    "results": [
        {
            "rank": 1,
            "title": "What's New In Python 3.13",
            "url": "https://docs.python.org/3.13/whatsnew/3.13.html",
            "snippet": "Summary of the release.",
            "domain": "docs.python.org",
        },
        {"rank": 2, "title": "Python 3.13", "url": "https://python.org/", "snippet": "", "domain": "python.org"},
    ],
    "error": None,
    "elapsed_ms": 812,
}


def _tool(handler) -> WebSearchTool:
    return WebSearchTool(
        base_url="http://web-agent", token="secret", timeout_s=5, transport=httpx.MockTransport(handler)
    )


def _responding(status_code: int, body: dict | None = None, requests: list | None = None):
    def handler(request: httpx.Request) -> httpx.Response:
        if requests is not None:
            requests.append(request)
        return httpx.Response(status_code, json=body or {})

    return handler


async def test_ok_lists_results_and_sends_auth() -> None:
    requests: list[httpx.Request] = []

    result = await _tool(_responding(200, OK_BODY, requests)).run({"query": " python 3.13 ", "region": "pl-pl"})

    assert not result.is_error
    assert "1. What's New In Python 3.13" in result.content
    assert result.content.count("https://docs.python.org/3.13/whatsnew/3.13.html") == 1
    assert "Summary of the release." in result.content
    [request] = requests
    assert request.url.path == "/v1/search"
    assert request.headers["authorization"] == "Bearer secret"
    assert json.loads(request.content) == {"query": "python 3.13", "max_results": 5, "region": "pl-pl"}


async def test_no_results_is_not_an_error() -> None:
    body = {**OK_BODY, "status": "no_results", "results": []}

    result = await _tool(_responding(200, body)).run({"query": "python 3.13"})

    assert not result.is_error
    assert "No web results" in result.content


async def test_blocked_tells_the_model_not_to_retry() -> None:
    body = {**OK_BODY, "status": "blocked", "results": [], "error": "DuckDuckGo served its bot challenge"}

    result = await _tool(_responding(200, body)).run({"query": "python 3.13"})

    assert result.is_error
    assert "bot challenge" in result.content
    assert "won't help" in result.content


async def test_search_error_is_reported() -> None:
    body = {**OK_BODY, "status": "error", "results": [], "error": "timeout"}

    result = await _tool(_responding(200, body)).run({"query": "python 3.13"})

    assert result.is_error
    assert "timeout" in result.content


async def test_upstream_http_error() -> None:
    result = await _tool(_responding(401)).run({"query": "x"})

    assert result.is_error
    assert "HTTP 401" in result.content


async def test_upstream_validation_error_becomes_input_error() -> None:
    with pytest.raises(ToolInputError, match="rejected"):
        await _tool(_responding(422, {"detail": "bad"})).run({"query": "x"})


async def test_unreachable_service() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("connection refused", request=request)

    result = await _tool(handler).run({"query": "x"})

    assert result.is_error
    assert "unreachable" in result.content


async def test_timeout() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ReadTimeout("slow", request=request)

    result = await _tool(handler).run({"query": "x"})

    assert result.is_error
    assert "timed out" in result.content


@pytest.mark.parametrize(
    "input",
    [
        {},
        {"query": ""},
        {"query": "   "},
        {"query": 5},
        {"query": "x" * 401},
        {"query": "x", "max_results": 0},
        {"query": "x", "max_results": 11},
        {"query": "x", "max_results": "3"},
        {"query": "x", "max_results": True},
        {"query": "x", "region": "Poland"},
    ],
)
async def test_invalid_input(input: dict) -> None:
    with pytest.raises(ToolInputError):
        await _tool(_responding(200, OK_BODY)).run(input)


async def test_registry_shows_invalid_input_to_the_model() -> None:
    registry = ToolRegistry([_tool(_responding(200, OK_BODY))], timeout_s=5, max_output_chars=1000)

    result = await registry.execute("web_search", {"query": ""})

    assert result.is_error
    assert result.content == "Invalid input: query must be a non-empty string"
