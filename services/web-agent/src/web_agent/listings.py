import asyncio
import json
import time

from pydantic import BaseModel, ValidationError

from web_agent.browser import BrowserManager, BrowserSession, origin_of
from web_agent.engines.common import screen
from web_agent.models import (
    DetailsStatus,
    Listing,
    ListingDetailsRequest,
    ListingDetailsResponse,
    ListingParam,
    ListingSearchRequest,
    ListingSearchResponse,
    ListingStatus,
)
from web_agent.portals import ListingPortal
from web_agent.portals.common import LocationMiss, names_place, parse_json
from web_agent.search import Audit, PageHook, PageVisitor, RateLimiter

# A lookup answer longer than this is not an autocomplete list.
LOOKUP_MAX_CHARS = 200_000
# How long a client-rendered page may take after its load event to show what we read.
READY_TIMEOUT_S = 5.0
READY_POLL_S = 0.25


class _LookupAnswer(BaseModel):
    status: int
    body: str


class ListingService:
    """Runs one portal search or one offer page as a Sense-Act-Verify loop (navigation + DOM).

    Like `SearchService`, never raises for page or network trouble: every outcome is a response
    the model can read, and a block ends the visit -- no retries. A search is a lookup of the
    locality in the portal's own autocomplete, then its results page, in one session and within
    one timeout. The portal is always given by code (`portals.get_portal`); the only URL from the
    model is an offer page, checked against the portal's offer pattern before this is called.
    """

    def __init__(self, manager: BrowserManager, limiter: RateLimiter, *, timeout_s: float) -> None:
        self._visitor = PageVisitor(manager, limiter, timeout_s=timeout_s)

    async def listing_search(
        self, request: ListingSearchRequest, portal: ListingPortal, *, on_page: PageHook | None = None
    ) -> ListingSearchResponse:
        started = time.monotonic()
        audit = Audit(engine=portal.name, page_url=portal.lookup_url(request.location))

        def respond(
            status: ListingStatus,
            *,
            results: list[Listing] | None = None,
            resolved_location: str | None = None,
            total_count: int | None = None,
            search_url: str | None = None,
            error: str | None = None,
        ) -> ListingSearchResponse:
            response = ListingSearchResponse(
                status=status,
                portal=portal.name,
                location=request.location,
                resolved_location=resolved_location,
                total_count=total_count,
                search_url=search_url,
                results=results or [],
                error=error,
                elapsed_ms=int((time.monotonic() - started) * 1000),
            )
            audit.log(response.status, response.results)
            return response

        async def visit(session: BrowserSession) -> ListingSearchResponse:
            audit.blocked_requests = session.blocked_requests
            # Lookup: the portal's autocomplete, fetched from a page on its own origin.
            page = await self._visitor.load(session, portal.lookup_page, portal)
            if page.blocked:
                return respond("blocked", error=page.blocked)
            answer = await _fetch(session, portal.lookup_url(request.location))
            if answer is None:
                return respond("error", error="unexpected answer from the portal's location lookup")
            if reason := portal.detect_block(answer.status, ""):
                session.keep_open_for_human()
                return respond("blocked", error=reason)
            match = portal.resolve_location(parse_json(answer.body), request.location)
            if match is None:
                return respond("error", error="unexpected answer from the portal's location lookup")
            if isinstance(match, LocationMiss):
                return respond("unknown_location", error=_miss(portal.name, request.location, match))
            resolved = screen(name=match.name)
            if resolved.withheld:
                return respond("error", error="unexpected answer from the portal's location lookup")

            # The results page.
            url = portal.url_for(request, match)
            audit.page_url = url

            def found(status: ListingStatus, **fields) -> ListingSearchResponse:
                return respond(status, resolved_location=resolved.texts["name"], search_url=url, **fields)

            page = await self._visitor.load(session, url, portal)
            if on_page is not None:
                await on_page(session)
            if page.blocked:
                return found("blocked", error=page.blocked)
            await _wait_ready(session, portal.ready_js)
            raw = await self._visitor.sense(session, portal.extract_js, audit)
            parsed = portal.parse(raw, request)
            if parsed.heading is not None and not names_place(parsed.heading, match.place):
                return found(
                    "unknown_location",
                    error=f"the results page did not show {resolved.texts['name']} (another place or a nationwide list)",
                )
            if parsed.results:
                return found("ok", results=parsed.results, total_count=parsed.total_count)
            if parsed.no_results or parsed.total_count == 0 or portal.is_no_results(page.text):
                return found("no_results", total_count=parsed.total_count)
            return found("error", error="unexpected page layout")

        return await self._visitor.run(
            portal.name, _origins(portal), visit, lambda error: respond("error", error=error)
        )

    async def listing_details(
        self, request: ListingDetailsRequest, portal: ListingPortal, *, on_page: PageHook | None = None
    ) -> ListingDetailsResponse:
        started = time.monotonic()
        url = portal.offer_url(request.url)
        audit = Audit(engine=portal.name, page_url=url or "")

        def respond(
            status: DetailsStatus,
            *,
            listing: Listing | None = None,
            params: list[ListingParam] | None = None,
            error: str | None = None,
        ) -> ListingDetailsResponse:
            response = ListingDetailsResponse(
                status=status,
                portal=portal.name,
                listing=listing,
                params=params or [],
                error=error,
                elapsed_ms=int((time.monotonic() - started) * 1000),
            )
            audit.log(response.status, [listing] if listing else [])
            return response

        if url is None:
            # The HTTP layer refuses these first; this only guards other callers.
            return respond("error", error=f"not an offer page of {portal.name}")

        async def visit(session: BrowserSession) -> ListingDetailsResponse:
            audit.blocked_requests = session.blocked_requests
            page = await self._visitor.load(session, url, portal)
            if on_page is not None:
                await on_page(session)
            if page.blocked:
                return respond("blocked", error=page.blocked)
            landed = await session.evaluate("location.href") or ""
            if portal.is_inactive(page.status, landed, page.text):
                return respond("inactive")
            await _wait_ready(session, portal.ready_js)
            raw = await self._visitor.sense(session, portal.details_js, audit)
            offer = portal.parse_details(raw, url)
            if offer.inactive:
                return respond("inactive")
            if offer.listing is None:
                return respond("error", error="unexpected page layout")
            return respond("ok", listing=offer.listing, params=offer.params)

        # The offer page's own origin: nieruchomosci-online.pl serves offers on per-town hosts.
        allowed = _origins(portal) | {origin_of(url)}
        return await self._visitor.run(portal.name, allowed, visit, lambda error: respond("error", error=error))


def _origins(portal: ListingPortal) -> frozenset[str]:
    """Read-only origin set: the portal's hosts and what it declares its pages need to render."""
    return frozenset(f"https://{host}:443" for host in portal.hosts) | portal.extra_origins


def _miss(portal: str, location: str, miss: LocationMiss) -> str:
    message = f'{portal} knows no locality "{location}"'
    if miss.suggestions:
        message += f"; it suggests: {', '.join(miss.suggestions)}"
    return message


async def _fetch(session: BrowserSession, url: str) -> _LookupAnswer | None:
    """GET `url` from the page, as the portal's own scripts do (same origin, same cookies)."""
    expression = f"""
        fetch({json.dumps(url)}, {{
            headers: {{"X-Requested-With": "XMLHttpRequest", "Accept": "application/json"}},
            credentials: "same-origin",
        }}).then(async (response) => ({{
            status: response.status,
            body: (await response.text()).slice(0, {LOOKUP_MAX_CHARS}),
        }}))
    """
    try:
        return _LookupAnswer.model_validate(await session.evaluate(expression))
    except ValidationError:
        return None


async def _wait_ready(session: BrowserSession, ready_js: str) -> None:
    """Until `ready_js` holds or `READY_TIMEOUT_S` passes; the caller then reads what is there."""
    if ready_js.strip() == "true":
        return
    try:
        async with asyncio.timeout(READY_TIMEOUT_S):
            while not await session.evaluate(f"Boolean({ready_js})"):
                await asyncio.sleep(READY_POLL_S)
    except TimeoutError:
        pass
