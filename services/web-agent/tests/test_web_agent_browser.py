"""End-to-end through a real Chromium, against saved DuckDuckGo pages served locally."""

import threading
import time
from collections.abc import AsyncGenerator, Iterator
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest
import pytest_asyncio

from web_agent.browser import BrowserError, BrowserManager
from web_agent.engines.duckduckgo import DuckDuckGoEngine
from web_agent.engines.searxng import SearxngEngine
from web_agent.models import SearchQuery
from web_agent.search import RateLimiter, SearchService

pytestmark = pytest.mark.browser

FIXTURES = Path(__file__).parent / "fixtures"
# path -> (HTTP status, fixture file); DuckDuckGo answers its bot challenge with 202.
PAGES = {
    "/results": (200, "ddg_results.html"),
    "/no_results": (200, "ddg_no_results.html"),
    "/blocked": (202, "ddg_blocked.html"),
    "/searxng_results": (200, "searxng_results.html"),
    "/searxng_no_results": (200, "searxng_no_results.html"),
}
HANG_S = 3


class _FixtureHandler(BaseHTTPRequestHandler):
    def do_GET(self) -> None:
        if self.path == "/hang":
            time.sleep(HANG_S)
        status, name = PAGES.get(self.path, (404, None))
        body = (FIXTURES / name).read_bytes() if name else b"not found"
        self.send_response(status)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        # Keep the saved pages from reaching out to duckduckgo.com (favicons, challenge images).
        self.send_header("Content-Security-Policy", "default-src 'self' 'unsafe-inline'")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, format: str, *args) -> None:
        pass


class LocalEngine(DuckDuckGoEngine):
    """DuckDuckGo's parsing, pointed at the local server: the query is the fixture path."""

    def __init__(self, base_url: str) -> None:
        self._base_url = base_url

    def url_for(self, query: SearchQuery, region: str) -> str:
        return f"{self._base_url}/{query.query}"


class LocalSearxngEngine(SearxngEngine):
    def url_for(self, query: SearchQuery, region: str) -> str:
        return f"{self._base_url}/{query.query}"


@pytest.fixture(scope="module")
def base_url() -> Iterator[str]:
    server = ThreadingHTTPServer(("127.0.0.1", 0), _FixtureHandler)
    server.daemon_threads = True
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    yield f"http://127.0.0.1:{server.server_port}"
    server.shutdown()


@pytest_asyncio.fixture
async def manager() -> AsyncGenerator[BrowserManager, None]:
    manager = BrowserManager(cdp_url=None, headless=True, navigation_timeout_s=10)
    try:
        await manager.start()
    except BrowserError as exc:
        pytest.skip(f"Chromium not available: {exc}")
    yield manager
    await manager.close()


def _service(manager: BrowserManager, *, timeout_s: float = 10) -> SearchService:
    limiter = RateLimiter(max_concurrent=2, min_interval_s=0)
    return SearchService(manager, limiter, timeout_s=timeout_s, default_region="pl-pl")


async def test_results_page(manager: BrowserManager, base_url: str) -> None:
    response = await _service(manager).search(SearchQuery(query="results", max_results=10), LocalEngine(base_url))

    assert response.status == "ok"
    assert len(response.results) >= 5
    for result in response.results:
        assert result.title
        assert result.url.startswith(("http://", "https://"))
        assert "duckduckgo.com" not in result.domain


async def test_no_results_page(manager: BrowserManager, base_url: str) -> None:
    response = await _service(manager).search(SearchQuery(query="no_results"), LocalEngine(base_url))

    assert response.status == "no_results"


async def test_blocked_page(manager: BrowserManager, base_url: str) -> None:
    response = await _service(manager).search(SearchQuery(query="blocked"), LocalEngine(base_url))

    assert response.status == "blocked"
    assert response.results == []
    assert response.error


async def test_unknown_layout_is_an_error_not_no_results(manager: BrowserManager, base_url: str) -> None:
    response = await _service(manager).search(SearchQuery(query="missing"), LocalEngine(base_url))

    assert (response.status, response.error) == ("error", "unexpected page layout")


async def test_timeout_returns_error_and_leaks_no_contexts(manager: BrowserManager, base_url: str) -> None:
    service = _service(manager, timeout_s=0.5)
    engine = LocalEngine(base_url)

    responses = [
        await service.search(SearchQuery(query="hang" if i % 4 == 0 else "results"), engine) for i in range(20)
    ]

    assert responses[0].status == "error"
    assert responses[0].error == "timeout"
    assert {r.status for r in responses[1:4]} == {"ok"}
    assert manager.open_contexts == 0
    assert manager._browser is not None and manager._browser.contexts == []


async def test_screenshot_hook(manager: BrowserManager, base_url: str) -> None:
    shots: list[bytes] = []

    async def capture(session) -> None:
        shots.append(await session.screenshot())

    await _service(manager).search(SearchQuery(query="results"), LocalEngine(base_url), on_page=capture)

    assert shots and shots[0].startswith(b"\x89PNG")


async def test_searxng_results_page(manager: BrowserManager, base_url: str) -> None:
    engine = LocalSearxngEngine(base_url)

    response = await _service(manager).search(SearchQuery(query="searxng_results", max_results=10), engine)

    assert response.status == "ok"
    assert len(response.results) == 10
    assert response.results[0].url == "https://en.wikipedia.org/wiki/Large_language_model"
    assert response.results[0].title == "Large language model - Wikipedia"
    assert response.results[0].snippet.startswith("A large language model (LLM)")


async def test_searxng_no_results_page(manager: BrowserManager, base_url: str) -> None:
    engine = LocalSearxngEngine(base_url)

    response = await _service(manager).search(SearchQuery(query="searxng_no_results"), engine)

    assert response.status == "no_results"
