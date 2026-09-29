"""The web-agent API behind api's real-estate tools (`plot_search`, `plot_details`).

`POST /v1/listing-search` takes `ListingSearchRequest` and answers `ListingSearchResponse`;
`POST /v1/listing-details` takes `ListingDetailsRequest` and answers `ListingDetailsResponse`. A
blocked or failed search is still HTTP 200, its outcome in `status`; an unknown portal or an offer
URL outside the portal is HTTP 422. `GET /v1/portals` answers `PortalsResponse`. All need the
internal bearer token (`contracts.auth`).

Money and areas are `Decimal`s and travel as JSON strings (Pydantic's default for `Decimal`), e.g.
`"290000"`, `"333.33"`: no float rounding on the way.

`offer_url` is the one check on a URL that comes from the model: web-agent runs it before opening
the page and api runs it before calling web-agent, from the same `OFFER_URL_PATTERNS`.
"""

import re
from datetime import date
from decimal import Decimal
from typing import Annotated, Literal
from urllib.parse import urlsplit

from pydantic import BaseModel, Field, StringConstraints

LISTING_SEARCH_PATH = "/v1/listing-search"
LISTING_DETAILS_PATH = "/v1/listing-details"
PORTALS_PATH = "/v1/portals"

DEFAULT_LISTING_RESULTS = 10
MAX_LISTING_RESULTS = 20
LOCATION_MAX_CHARS = 100
OFFER_URL_MAX_CHARS = 2000
# The structured parameters of one offer page, as the portal lists them.
PARAM_VALUE_MAX_CHARS = 150
MAX_PARAMS = 20

# Only what every portal can do; price per m² is computed by the caller.
ListingSort = Literal["relevance", "newest", "price_asc"]
ListingStatus = Literal["ok", "no_results", "unknown_location", "blocked", "error"]
DetailsStatus = Literal["ok", "inactive", "blocked", "error"]
Currency = Literal["PLN", "EUR"]

Location = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=LOCATION_MAX_CHARS)]
OfferUrl = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=OFFER_URL_MAX_CHARS)]

# portal -> (hosts its offer pages live on, path of an offer page). A host pattern is a full match
# of the lowercased hostname; nieruchomosci-online.pl serves offers on per-town subdomains.
OFFER_URL_PATTERNS: dict[str, tuple[re.Pattern[str], re.Pattern[str]]] = {
    "nieruchomosci-online.pl": (
        re.compile(r"[a-z0-9-]+\.nieruchomosci-online\.pl"),
        re.compile(r"/(?:[a-z0-9,-]+/)?\d+\.html"),
    ),
    "nehnutelnosti.sk": (
        re.compile(r"(?:www\.)?nehnutelnosti\.sk"),
        re.compile(r"/detail/[A-Za-z0-9_-]+(?:/[a-z0-9-]+)?/?"),
    ),
    "otodom.pl": (
        re.compile(r"(?:www\.)?otodom\.pl"),
        re.compile(r"/pl/oferta/[A-Za-z0-9-]+"),
    ),
    "olx.pl": (
        re.compile(r"(?:www\.|m\.)?olx\.pl"),
        re.compile(r"/d/oferta/[A-Za-z0-9-]+\.html"),
    ),
    "adresowo.pl": (
        re.compile(r"(?:www\.)?adresowo\.pl"),
        re.compile(r"/o/[A-Za-z0-9-]+"),
    ),
}


def offer_url(portal: str, url: str) -> str | None:
    """`url` as `https://<host><path>` if it is an offer page of `portal` (https, the portal's host,
    its offer path, no credentials or port), without query string and fragment; else None."""
    patterns = OFFER_URL_PATTERNS.get(portal)
    if patterns is None:
        return None
    host_pattern, path_pattern = patterns
    try:
        parsed = urlsplit(url.strip())
        port = parsed.port
    except ValueError:
        return None
    host = (parsed.hostname or "").lower()
    if parsed.scheme != "https" or parsed.username is not None or parsed.password is not None or port is not None:
        return None
    if not host_pattern.fullmatch(host) or not path_pattern.fullmatch(parsed.path):
        return None
    return f"https://{host}{parsed.path}"


class ListingSearchRequest(BaseModel):
    """One portal, one locality: everything a single search needs; the tool keeps no state."""

    portal: str
    # As a person writes it, e.g. "Białka Tatrzańska", "Oravská Lesná".
    location: Location
    # In the portal's currency: PLN on .pl portals, EUR on .sk ones.
    max_price: int | None = Field(default=None, ge=1)
    min_area_m2: int | None = Field(default=None, ge=1)
    max_results: int = Field(default=DEFAULT_LISTING_RESULTS, ge=1, le=MAX_LISTING_RESULTS)
    sort: ListingSort = "newest"


class Listing(BaseModel):
    rank: int
    title: str
    url: str
    # As the portal shows it, e.g. "Czarna Góra, małopolskie".
    location: str | None = None
    price: Decimal | None = None
    currency: Currency | None = None
    area_m2: Decimal | None = None
    # From the portal, or computed when both price and area are known.
    price_per_m2: Decimal | None = None
    # Verbatim from the portal ("budowlana", "rolna", "stavebný pozemok"); never translated or guessed.
    plot_type: str | None = None
    listed_at: date | None = None
    updated_at: date | None = None
    private_seller: bool | None = None
    # `sponsored` (a promoted, still real offer) and the prompt-injection signals.
    flags: list[str] = []
    # Texts dropped as suspected prompt injection; only the URL is kept, to report it.
    withheld: bool = False


class ListingSearchResponse(BaseModel):
    status: ListingStatus
    portal: str
    # As in the request.
    location: str
    # What the portal matched the location to, e.g. "Groń, gm. Bukowina Tatrzańska".
    resolved_location: str | None = None
    # The portal's own count of matching offers; `results` holds at most its first page.
    total_count: int | None = None
    # The results page, for a person to open.
    search_url: str | None = None
    results: list[Listing] = []
    error: str | None = None
    elapsed_ms: int


class ListingDetailsRequest(BaseModel):
    portal: str
    # An offer page of `portal` (see `offer_url`), e.g. a URL from a search result.
    url: OfferUrl


class ListingParam(BaseModel):
    """One row of an offer page's parameter table, e.g. "Media" -> "prąd, woda"."""

    name: str
    value: str


class ListingDetailsResponse(BaseModel):
    status: DetailsStatus
    portal: str
    listing: Listing | None = None
    params: list[ListingParam] = []
    error: str | None = None
    elapsed_ms: int


class PortalsResponse(BaseModel):
    portals: list[str]
