from dataclasses import dataclass, field
from typing import ClassVar, Protocol
from urllib.parse import urljoin

from contracts.listings import offer_url
from pydantic import ValidationError

from web_agent.engines.common import MarkerEngine
from web_agent.models import Currency, Listing, ListingParam, ListingSearchRequest
from web_agent.portals.common import (
    Clock,
    LocationMatch,
    LocationMiss,
    RawListing,
    build_listing,
    build_listings,
    build_params,
    parse_count,
    raw_details,
    raw_page,
    today_utc,
)

# An offer page that is gone: removed (404) or ended (410).
GONE_STATUSES = frozenset({404, 410})


@dataclass(frozen=True)
class ListingPage:
    """A parsed results page: its offers and what the page says about itself."""

    results: list[Listing] = field(default_factory=list)
    # The locality the page says it shows, e.g. its heading; None if the page doesn't say.
    heading: str | None = None
    total_count: int | None = None
    # The page's own "nothing found" state.
    no_results: bool = False


@dataclass(frozen=True)
class OfferPage:
    """A parsed offer page: the offer and its parameter table, or that the offer is gone."""

    listing: Listing | None = None
    params: list[ListingParam] = field(default_factory=list)
    inactive: bool = False


class ListingPortal(Protocol):
    """One real-estate portal's pages: where to go, what to read, how to tell a block apart.

    Pure data and functions -- no browser here, so every portal is testable on saved pages. The
    browser work (lookup, navigate, evaluate, verify) lives in `listings.ListingService`.
    """

    name: str
    # Hosts the portal's pages and lookup live on; their https origins are allowed.
    hosts: frozenset[str]
    # Origins besides `hosts` a page needs to render (e.g. a script CDN); only code sets this.
    extra_origins: frozenset[str]
    block_settle_s: float
    currency: Currency
    # A light page on the lookup's origin (robots.txt), opened so the lookup is a same-origin fetch.
    lookup_page: str
    # JS expression, truthy once a page rendered what `extract_js`/`details_js` read ("true" for
    # server-rendered pages).
    ready_js: str
    # JS expression evaluated on a results page; returns a `common.RawPage`.
    extract_js: str
    # JS expression evaluated on an offer page; returns a `common.RawDetails`.
    details_js: str

    def lookup_url(self, location: str) -> str:
        """The portal's own locality autocomplete for `location`, answering JSON."""
        ...

    def resolve_location(self, lookup: object, location: str) -> LocationMatch | LocationMiss | None:
        """The locality `location` names, from the parsed lookup answer; None if the answer has
        an unexpected shape."""
        ...

    def url_for(self, request: ListingSearchRequest, location: LocationMatch) -> str: ...

    def parse(self, raw: object, request: ListingSearchRequest) -> ListingPage: ...

    def parse_details(self, raw: object, url: str) -> OfferPage: ...

    def detect_block(self, status: int | None, text: str) -> str | None: ...

    def is_no_results(self, text: str) -> bool: ...

    def is_inactive(self, status: int | None, url: str, text: str) -> bool:
        """Whether the offer page at `url` (where the browser ended up) says it is gone."""
        ...

    def offer_url(self, url: str) -> str | None:
        """`url` if it is an offer page of this portal (see `contracts.listings.offer_url`)."""
        ...


class MarkerPortal(MarkerEngine):
    """The `ListingPortal` parts every portal shares: page markers, offer URLs, and turning
    `RawPage`/`RawDetails` into listings. Portals set their data and add what's their own
    (`lookup_url`, `resolve_location`, `url_for`, the JS)."""

    name: str
    base_url: str
    hosts: frozenset[str]
    currency: Currency
    ready_js: str = "true"
    extract_js: str
    details_js: str
    inactive_markers: tuple[str, ...] = ()
    # Sorts the portal's URL can't express, done on the first page's offers instead.
    local_sorts: ClassVar[frozenset[str]] = frozenset()

    def __init__(self, *, today: Clock = today_utc) -> None:
        self._today = today

    @property
    def lookup_page(self) -> str:
        return f"{self.base_url}/robots.txt"

    def offer_url(self, url: str) -> str | None:
        return offer_url(self.name, url)

    def listing_url(self, href: str) -> str | None:
        """A result's link as an offer URL of this portal; relative links are resolved first."""
        return self.offer_url(urljoin(f"{self.base_url}/", href.strip()))

    def parse(self, raw: object, request: ListingSearchRequest) -> ListingPage:
        page = raw_page(raw)
        if page is None:
            return ListingPage()
        results = build_listings(page.entries, request, self.listing_url, currency=self.currency, today=self._today())
        if request.sort in self.local_sorts:
            results = _sorted_by_price(results)
        return ListingPage(
            results=results,
            heading=page.heading or None,
            total_count=parse_count(page.count),
            no_results=page.no_results,
        )

    def parse_details(self, raw: object, url: str) -> OfferPage:
        details = raw_details(raw)
        if details is None:
            return OfferPage()
        if details.inactive:
            return OfferPage(inactive=True)
        if details.offer is None:
            return OfferPage()
        try:
            entry = RawListing.model_validate(details.offer)
        except ValidationError:
            return OfferPage()
        listing = build_listing(entry, url, rank=1, currency=self.currency, today=self._today())
        if listing is None:
            return OfferPage()
        params, flags = build_params(details.params)
        if flags and not listing.withheld:
            # A parameter looked like instructions aimed at the model: withhold the whole offer.
            listing = Listing(rank=1, title="", url=listing.url, flags=[*listing.flags, *flags], withheld=True)
        return OfferPage(listing=listing, params=[] if listing.withheld else params)

    def is_inactive(self, status: int | None, url: str, text: str) -> bool:
        if status in GONE_STATUSES:
            return True
        # A removed offer often redirects to a results or home page.
        if self.offer_url(url) is None:
            return True
        return any(marker in text for marker in self.inactive_markers)


def _sorted_by_price(results: list[Listing]) -> list[Listing]:
    """Cheapest first, offers without a price last; ranks renumbered."""
    ordered = sorted(results, key=lambda listing: (listing.price is None, listing.price or 0))
    return [listing.model_copy(update={"rank": rank}) for rank, listing in enumerate(ordered, 1)]
