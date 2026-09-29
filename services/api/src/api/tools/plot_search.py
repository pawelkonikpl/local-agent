from collections.abc import Sequence
from decimal import Decimal
from typing import get_args

from contracts.listings import (
    DEFAULT_LISTING_RESULTS,
    LISTING_SEARCH_PATH,
    MAX_LISTING_RESULTS,
    Listing,
    ListingSearchRequest,
    ListingSearchResponse,
    ListingSort,
)
from contracts.web_agent import INJECTION_FLAGS, SPONSORED_FLAG

from api.tools.base import ToolInputError, ToolResult
from api.tools.site_search import SITE_AGENT_DOWN
from api.tools.web_agent import WITHHELD_NOTE, WebAgentClient, WebAgentTool, spotlight

SORTS: tuple[ListingSort, ...] = get_args(ListingSort)
MISSING = "?"
NARROW_NOTE = (
    "The portal has more listings than shown (only its first page is read): narrow the search "
    "with max_price or min_area_m2 rather than asking for more results."
)
PLOT_TYPE_NOTE = (
    "Prices and details are read from the portal at search time. The plot type is what the portal "
    "states ('rolna' or 'rekreačný pozemok' is not a building plot); '?' means the portal did not "
    "show that field. For utilities, access road and other parameters call plot_details with the "
    "listing URL."
)


class PlotSearchTool(WebAgentTool[ListingSearchRequest, ListingSearchResponse]):
    """`plot_search`: plots for sale in one locality on one real-estate portal.

    Served by `site-agent` (the headful Chrome that also serves `site_search`). Stateless: every
    call carries the portal, the locality and the filters; budgets, regions and comparisons with
    earlier searches belong to whoever calls it.
    """

    name = "plot_search"
    request_model = ListingSearchRequest
    response_model = ListingSearchResponse
    path = LISTING_SEARCH_PATH
    label = "Plot search"
    unreachable_message = SITE_AGENT_DOWN

    def __init__(self, client: WebAgentClient, *, portals: Sequence[str], view_url: str) -> None:
        if not portals:
            raise ValueError("plot_search needs at least one portal")
        super().__init__(client)
        self._portals = tuple(portals)
        self._view_url = view_url
        self.description = (
            "Searches one real-estate portal for building plots for sale in one town or village "
            f"({_by_country(self._portals)}) and returns listings with price, area, price per m², plot type, dates and "
            "URL. One call = one portal + one locality; to survey a region, call it for each locality "
            "and each portal, never with several places in `location`. Use Slovak place names for "
            "Slovak portals. Plot type is what the portal states: 'rolna' / 'rekreačný' is not a "
            "building plot. The listing's description is not read."
        )
        self.input_schema = {
            "type": "object",
            "properties": {
                "portal": {"type": "string", "enum": list(self._portals), "description": "The portal to search."},
                "location": {
                    "type": "string",
                    "description": "One town or village, as written locally, e.g. 'Białka Tatrzańska', 'Oravská Lesná'.",
                },
                "max_price": {
                    "type": "integer",
                    "minimum": 1,
                    "description": "Highest total price, in the portal's currency: PLN for .pl portals, EUR for .sk.",
                },
                "min_area_m2": {"type": "integer", "minimum": 1, "description": "Smallest plot area in m²."},
                "max_results": {
                    "type": "integer",
                    "minimum": 1,
                    "maximum": MAX_LISTING_RESULTS,
                    "description": f"How many listings to return, 1-{MAX_LISTING_RESULTS}. Defaults to {DEFAULT_LISTING_RESULTS}.",
                },
                "sort": {
                    "type": "string",
                    "enum": list(SORTS),
                    "description": "Order of listings: newest (default), cheapest first, or the portal's relevance.",
                },
            },
            "required": ["portal", "location"],
            "additionalProperties": False,
        }

    def _parse(self, input: dict) -> ListingSearchRequest:
        request = super()._parse(input)
        if request.portal not in self._portals:
            raise ToolInputError(f"portal must be one of: {', '.join(self._portals)}")
        return request

    def _format(self, request: ListingSearchRequest, response: ListingSearchResponse) -> ToolResult:
        where = response.resolved_location or request.location
        match response.status:
            case "ok":
                lines = [self._heading(request, response)]
                for listing in response.results:
                    lines.extend(listing_lines(listing))
                if response.total_count is not None and response.total_count > len(response.results):
                    lines.append(NARROW_NOTE)
                if any(listing.withheld for listing in response.results):
                    lines.append(WITHHELD_NOTE)
                lines.append(PLOT_TYPE_NOTE)
                return ToolResult(spotlight("\n".join(lines), source=f"plot_search:{request.portal}"))
            case "no_results":
                return ToolResult(
                    f"No plots for sale on {request.portal} in {where} matching {_filters(request)}. "
                    f"Results page: {response.search_url or MISSING}"
                )
            case "unknown_location":
                return ToolResult(
                    f'{request.portal} does not recognise "{request.location}" as a locality '
                    f"({response.error or 'no match'}). Try the gmina (municipality) or a neighbouring "
                    "village instead; on Slovak portals use the Slovak name."
                )
            case "blocked":
                return ToolResult(
                    f"{request.portal} asks for human verification ({response.error}). Do not retry "
                    f"yourself: ask the user to solve the check on the site browser's screen at "
                    f"{self._view_url} and then repeat the request.",
                    is_error=True,
                )
            case _:
                return ToolResult(
                    f"Plot search on {request.portal} failed: {response.error or 'unknown error'}. If it "
                    "keeps failing, ask the user to restart the site-agent container "
                    "(`podman-compose restart site-agent`).",
                    is_error=True,
                )

    def _heading(self, request: ListingSearchRequest, response: ListingSearchResponse) -> str:
        total = response.total_count if response.total_count is not None else MISSING
        return (
            f"Plots for sale on {request.portal} in {response.resolved_location or request.location}: "
            f"{total} listings on the portal, showing {len(response.results)}. Filters: "
            f"{_filters(request)}, sort: {request.sort}. Results page: {response.search_url or MISSING}"
        )


def listing_lines(listing: Listing) -> list[str]:
    """Two lines per listing: the facts, every missing one as '?', then the URL. A withheld
    listing keeps only its URL."""
    if listing.withheld:
        injection = [flag for flag in listing.flags if flag in INJECTION_FLAGS]
        return [
            f"{listing.rank}. [content withheld: looked like instructions aimed at an AI ({', '.join(injection)})]",
            f"   {listing.url}",
        ]
    currency = listing.currency or MISSING
    facts = [
        f"{amount(listing.price)} {currency}" if listing.price is not None else MISSING,
        f"{amount(listing.area_m2)} m²" if listing.area_m2 is not None else MISSING,
        f"{amount(listing.price_per_m2)} {currency}/m²" if listing.price_per_m2 is not None else MISSING,
        listing.plot_type or MISSING,
        f"dodano {listing.listed_at or MISSING}",
    ]
    if listing.updated_at and listing.updated_at != listing.listed_at:
        facts.append(f"odświeżono {listing.updated_at}")
    facts.append(_seller(listing.private_seller))
    line = f"{listing.rank}. {listing.location or MISSING} — {' · '.join(facts)} · \"{listing.title}\""
    if SPONSORED_FLAG in listing.flags:
        line += " [sponsored]"
    return [line, f"   {listing.url}"]


def amount(value: Decimal) -> str:
    """290000 -> "290 000"; small fractional amounts (per-m² prices in EUR) keep two decimals."""
    if value == value.to_integral_value() or value >= 100:
        return f"{int(value.quantize(Decimal(1))):,}".replace(",", " ")
    return f"{value:.2f}"


def _by_country(portals: Sequence[str]) -> str:
    """"Poland: otodom.pl, olx.pl; Slovakia: nehnutelnosti.sk"."""
    countries = {"Poland": ".pl", "Slovakia": ".sk"}
    parts = []
    for country, suffix in countries.items():
        if names := [portal for portal in portals if portal.endswith(suffix)]:
            parts.append(f"{country}: {', '.join(names)}")
    return "; ".join(parts)


def _seller(private: bool | None) -> str:
    if private is None:
        return MISSING
    return "prywatne" if private else "biuro"


def _filters(request: ListingSearchRequest) -> str:
    currency = "EUR" if request.portal.endswith(".sk") else "PLN"
    price = f"max price {amount(Decimal(request.max_price))} {currency}" if request.max_price else "no max price"
    area = f"min area {request.min_area_m2} m²" if request.min_area_m2 else "no min area"
    return f"{price}, {area}"
