import asyncio
import hashlib
import json
import logging
import time
from collections import defaultdict
from collections.abc import AsyncGenerator, Awaitable, Callable
from contextlib import asynccontextmanager
from dataclasses import dataclass, field

from web_agent.browser import BrowserError, BrowserManager, BrowserSession, origin_of
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
        url = engine.url_for(query, query.region or self._default_region)
        audit = _Audit(engine=engine.name, page_url=url)

        def respond(status: SearchStatus, *, results=(), error: str | None = None) -> SearchResponse:
            response = SearchResponse(
                status=status,
                engine=engine.name,
                query=query.query,
                results=list(results),
                error=error,
                elapsed_ms=int((time.monotonic() - started) * 1000),
            )
            audit.log(response)
            return response

        console_errors: list[str] = []
        # Read-only origin set: the results page and what the engine declares it needs to render.
        allowed_origins = frozenset({origin_of(url)}) | engine.extra_origins
        try:
            async with asyncio.timeout(self._timeout_s):
                async with (
                    self._limiter.slot(engine.name),
                    self._manager.session(allowed_origins=allowed_origins) as session,
                ):
                    console_errors = session.console_errors
                    audit.blocked_requests = session.blocked_requests
                    # Act
                    outcome = await session.navigate(url)
                    if on_page is not None:
                        await on_page(session)
                    # Verify: the document status comes from the Network domain, the text from the DOM.
                    text = await session.text_content()
                    if reason := engine.detect_block(outcome.status, text):
                        return respond("blocked", error=reason)
                    # Sense
                    raw = await session.evaluate(engine.extract_js)
                    audit.content_sha256 = hashlib.sha256(
                        json.dumps(raw, sort_keys=True, ensure_ascii=False).encode()
                    ).hexdigest()
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


@dataclass
class _Audit:
    """One `search_audit` log line per search, whatever its outcome: what was read and decided."""

    engine: str
    page_url: str
    content_sha256: str | None = None
    blocked_requests: list[str] = field(default_factory=list)

    def log(self, response: SearchResponse) -> None:
        flags = sorted({flag for result in response.results for flag in result.flags})
        logger.info(
            "search_audit %s",
            json.dumps(
                {
                    "engine": self.engine,
                    "status": response.status,
                    "page_url": self.page_url,
                    "content_sha256": self.content_sha256,
                    "results": len(response.results),
                    "withheld": sum(result.withheld for result in response.results),
                    "flags": flags,
                    "blocked_requests": self.blocked_requests,
                },
                ensure_ascii=False,
            ),
        )


def build_search_service(settings: Settings) -> tuple[BrowserManager, SearchService]:
    manager = BrowserManager(
        cdp_url=settings.cdp_url,
        headless=settings.headless,
        navigation_timeout_s=settings.navigation_timeout_s,
        proxy_url=settings.proxy_url,
        proxy_bypass=settings.proxy_bypass,
    )
    limiter = RateLimiter(max_concurrent=settings.max_concurrent_searches, min_interval_s=settings.min_interval_s)
    service = SearchService(
        manager, limiter, timeout_s=settings.search_timeout_s, default_region=settings.default_region
    )
    return manager, service
