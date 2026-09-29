import asyncio
import hashlib
import json
import logging
import time
from collections import defaultdict
from collections.abc import AsyncGenerator, Awaitable, Callable, Iterable
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from typing import Any, Protocol

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


class BlockDetector(Protocol):
    """What `PageVisitor.load` needs to know about a site: how it looks when it refuses us."""

    block_settle_s: float

    def detect_block(self, status: int | None, text: str) -> str | None: ...


@dataclass(frozen=True)
class PageLoad:
    status: int | None
    # The page's visible text, for markers.
    text: str
    # Why the site refused to serve the page, or None.
    blocked: str | None


class PageVisitor:
    """One visit to a site, shared by every service driving the browser: the rate-limit slot, a
    session with a fixed origin set, one overall timeout, and every page or network failure
    turned into an outcome the caller describes -- never an exception.
    """

    def __init__(self, manager: BrowserManager, limiter: RateLimiter, *, timeout_s: float) -> None:
        self._manager = manager
        self._limiter = limiter
        self._timeout_s = timeout_s

    async def run[T](
        self,
        key: str,
        allowed_origins: frozenset[str],
        visit: Callable[[BrowserSession], Awaitable[T]],
        on_failure: Callable[[str], T],
    ) -> T:
        """`visit` in a new session, rate-limited per `key` (also the hold key of a tab kept for a
        person); a timeout or `BrowserError` becomes `on_failure(<reason>)`."""
        console_errors: list[str] = []
        try:
            async with asyncio.timeout(self._timeout_s):
                async with (
                    self._limiter.slot(key),
                    self._manager.session(allowed_origins=allowed_origins, hold_key=key) as session,
                ):
                    console_errors = session.console_errors
                    return await visit(session)
        except TimeoutError:
            logger.warning("Visit timed out: key=%s console_errors=%s", key, console_errors)
            return on_failure("timeout")
        except BrowserError as exc:
            logger.warning("Visit failed: key=%s error=%s console_errors=%s", key, exc, console_errors)
            return on_failure(str(exc))

    async def load(self, session: BrowserSession, url: str, site: BlockDetector) -> PageLoad:
        """Act: open `url`. Verify: the document status comes from the Network domain, the text
        from the DOM. A block the site's JS check may clear on its own is waited out once; a block
        that stays leaves the tab for a person to solve (site browser only), never us."""
        outcome = await session.navigate(url)
        status = outcome.status
        text = await session.text_content()
        reason = site.detect_block(status, text)
        if reason and site.block_settle_s > 0:
            reloaded = await session.wait_for_reload(site.block_settle_s)
            if reloaded is not None:
                status = reloaded.status
                text = await session.text_content()
                reason = site.detect_block(status, text)
        if reason:
            session.keep_open_for_human()
        return PageLoad(status=status, text=text, blocked=reason)

    @staticmethod
    async def sense(session: BrowserSession, expression: str, audit: "Audit") -> Any:
        """Sense: evaluate `expression` in the page; the audit keeps a hash of what was read."""
        raw = await session.evaluate(expression)
        audit.content_sha256 = hashlib.sha256(json.dumps(raw, sort_keys=True, ensure_ascii=False).encode()).hexdigest()
        return raw


class SearchService:
    """Runs one search as a Sense-Act-Verify loop on rung 1 of the ladder (navigation + DOM).

    `search` never raises for page or network trouble: every outcome is a `SearchResponse` the
    model can read. A block ends the search -- no retries, no climbing the ladder. `engine` is a
    web search engine or a site searched through its own search page (`web_agent.sites`).
    """

    def __init__(
        self,
        manager: BrowserManager,
        limiter: RateLimiter,
        *,
        timeout_s: float,
        default_region: str,
    ) -> None:
        self._visitor = PageVisitor(manager, limiter, timeout_s=timeout_s)
        self._default_region = default_region

    async def search(
        self, query: SearchQuery, engine: SearchEngine, *, on_page: PageHook | None = None
    ) -> SearchResponse:
        started = time.monotonic()
        url = engine.url_for(query, query.region or self._default_region)
        audit = Audit(engine=engine.name, page_url=url)

        def respond(status: SearchStatus, *, results=(), error: str | None = None) -> SearchResponse:
            response = SearchResponse(
                status=status,
                engine=engine.name,
                query=query.query,
                results=list(results),
                error=error,
                elapsed_ms=int((time.monotonic() - started) * 1000),
            )
            audit.log(response.status, response.results)
            return response

        async def visit(session: BrowserSession) -> SearchResponse:
            audit.blocked_requests = session.blocked_requests
            page = await self._visitor.load(session, url, engine)
            if on_page is not None:
                await on_page(session)
            if page.blocked:
                return respond("blocked", error=page.blocked)
            raw = await self._visitor.sense(session, engine.extract_js, audit)
            results = engine.parse(raw if isinstance(raw, list) else [], query.max_results)
            if results:
                return respond("ok", results=results)
            if engine.is_no_results(page.text):
                return respond("no_results")
            return respond("error", error="unexpected page layout")

        # Read-only origin set: the results page and what the engine declares it needs to render.
        allowed_origins = frozenset({origin_of(url)}) | engine.extra_origins
        return await self._visitor.run(
            engine.name, allowed_origins, visit, lambda error: respond("error", error=error)
        )


class Audited(Protocol):
    flags: list[str]
    withheld: bool


@dataclass
class Audit:
    """One `search_audit` log line per search, whatever its outcome: what was read and decided."""

    engine: str
    page_url: str
    content_sha256: str | None = None
    blocked_requests: list[str] = field(default_factory=list)

    def log(self, status: str, results: Iterable[Audited]) -> None:
        results = list(results)
        flags = sorted({flag for result in results for flag in result.flags})
        logger.info(
            "search_audit %s",
            json.dumps(
                {
                    "engine": self.engine,
                    "status": status,
                    "page_url": self.page_url,
                    "content_sha256": self.content_sha256,
                    "results": len(results),
                    "withheld": sum(result.withheld for result in results),
                    "flags": flags,
                    "blocked_requests": self.blocked_requests,
                },
                ensure_ascii=False,
            ),
        )


def build_browser(settings: Settings) -> tuple[BrowserManager, RateLimiter]:
    """The one browser and the one rate limiter every service of this process shares."""
    manager = BrowserManager(
        cdp_url=settings.cdp_url,
        headless=settings.headless,
        navigation_timeout_s=settings.navigation_timeout_s,
        proxy_url=settings.proxy_url,
        proxy_bypass=settings.proxy_bypass,
        reuse_default_context=settings.reuse_default_context,
    )
    limiter = RateLimiter(max_concurrent=settings.max_concurrent_searches, min_interval_s=settings.min_interval_s)
    return manager, limiter
