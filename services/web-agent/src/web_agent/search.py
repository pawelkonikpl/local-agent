import asyncio
import logging
import time
from collections import defaultdict
from collections.abc import AsyncGenerator, Awaitable, Callable
from contextlib import asynccontextmanager

from web_agent.browser import BrowserError, BrowserManager, BrowserSession
from web_agent.config import Settings
from web_agent.engines import SearchEngine
from web_agent.models import SearchQuery, SearchResponse, SearchStatus

logger = logging.getLogger(__name__)

PageHook = Callable[[BrowserSession], Awaitable[None]]


class RateLimiter:
    """At most `max_concurrent` searches at once, and `min_interval_s` between two requests to
    the same engine."""

    def __init__(
        self, *, max_concurrent: int, min_interval_s: float, clock: Callable[[], float] = time.monotonic
    ) -> None:
        self._semaphore = asyncio.Semaphore(max_concurrent)
        self._min_interval_s = min_interval_s
        self._clock = clock
        self._locks: defaultdict[str, asyncio.Lock] = defaultdict(asyncio.Lock)
        self._last_request: dict[str, float] = {}

    @asynccontextmanager
    async def slot(self, key: str) -> AsyncGenerator[None]:
        async with self._semaphore:
            async with self._locks[key]:
                last = self._last_request.get(key)
                if last is not None:
                    wait = last + self._min_interval_s - self._clock()
                    if wait > 0:
                        await asyncio.sleep(wait)
                self._last_request[key] = self._clock()
            yield


class SearchService:
    """Runs one search as a Sense-Act-Verify loop on rung 1 of the ladder (navigation + DOM).

    `search` never raises for page or network trouble: every outcome is a `SearchResponse` the
    model can read. A block ends the search -- no retries, no climbing the ladder.
    """

    def __init__(
        self,
        manager: BrowserManager,
        limiter: RateLimiter,
        *,
        timeout_s: float,
        default_region: str,
    ) -> None:
        self._manager = manager
        self._limiter = limiter
        self._timeout_s = timeout_s
        self._default_region = default_region

    async def search(
        self, query: SearchQuery, engine: SearchEngine, *, on_page: PageHook | None = None
    ) -> SearchResponse:
        started = time.monotonic()

        def respond(status: SearchStatus, *, results=(), error: str | None = None) -> SearchResponse:
            return SearchResponse(
                status=status,
                engine=engine.name,
                query=query.query,
                results=list(results),
                error=error,
                elapsed_ms=int((time.monotonic() - started) * 1000),
            )

        console_errors: list[str] = []
        try:
            async with asyncio.timeout(self._timeout_s):
                async with self._limiter.slot(engine.name), self._manager.session() as session:
                    console_errors = session.console_errors
                    # Act
                    outcome = await session.navigate(engine.url_for(query, query.region or self._default_region))
                    if on_page is not None:
                        await on_page(session)
                    # Verify: the document status comes from the Network domain, the text from the DOM.
                    text = await session.text_content()
                    if reason := engine.detect_block(outcome.status, text):
                        return respond("blocked", error=reason)
                    # Sense
                    raw = await session.evaluate(engine.extract_js)
                    results = engine.parse(raw if isinstance(raw, list) else [], query.max_results)
                    if results:
                        return respond("ok", results=results)
                    if engine.is_no_results(text):
                        return respond("no_results")
                    return respond("error", error="unexpected page layout")
        except TimeoutError:
            logger.warning("Search timed out: engine=%s console_errors=%s", engine.name, console_errors)
            return respond("error", error="timeout")
        except BrowserError as exc:
            logger.warning("Search failed: engine=%s error=%s console_errors=%s", engine.name, exc, console_errors)
            return respond("error", error=str(exc))


def build_search_service(settings: Settings) -> tuple[BrowserManager, SearchService]:
    manager = BrowserManager(
        cdp_url=settings.cdp_url,
        headless=settings.headless,
        navigation_timeout_s=settings.navigation_timeout_s,
    )
    limiter = RateLimiter(max_concurrent=settings.max_concurrent_searches, min_interval_s=settings.min_interval_s)
    service = SearchService(
        manager, limiter, timeout_s=settings.search_timeout_s, default_region=settings.default_region
    )
    return manager, service
