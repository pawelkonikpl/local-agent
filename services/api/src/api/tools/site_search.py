from collections.abc import Sequence
from typing import get_args

from contracts.web_agent import (
    DEFAULT_MAX_RESULTS,
    MAX_RESULTS_LIMIT,
    SITE_SEARCH_PATH,
    SearchResponse,
    SiteSearchRequest,
    SortOrder,
)

from api.tools.base import ToolInputError
from api.tools.web_agent import SearchTool, WebAgentClient

SORT_ORDERS: tuple[SortOrder, ...] = get_args(SortOrder)
# Shops pin sponsored and promoted offers above a price-sorted list.
PRICE_SORT_NOTE = (
    "Sorted by price, but the shop pins sponsored and promoted offers on top: compare the prices "
    "yourself and check that an offer is the product itself, not an accessory."
)
SITE_AGENT_DOWN = (
    "The shop search service (the site-agent container) isn't running; ask the user to start it "
    "with `podman-compose up -d site-agent`."
)


class SiteSearchTool(SearchTool[SiteSearchRequest]):
    """`site_search`: offers from a shop's own search page (title, price, URL).

    Served by a separate web-agent instance (`site-agent`) driving a real, headful Chrome, because
    shops like Allegro block a headless browser. The model only names a site from a fixed list, a
    query and a sort order; web-agent builds the URL.
    """

    name = "site_search"
    request_model = SiteSearchRequest
    path = SITE_SEARCH_PATH
    label = "Site search"
    unreachable_message = SITE_AGENT_DOWN

    def __init__(self, client: WebAgentClient, *, sites: Sequence[str], view_url: str) -> None:
        if not sites:
            raise ValueError("site_search needs at least one site")
        super().__init__(client)
        self._sites = tuple(sites)
        self._view_url = view_url
        names = ", ".join(self._sites)
        self.description = (
            f"Searches directly in the search engine of an online shop ({names}) and returns a "
            "numbered list of offers: title, price and URL. Call it whenever the user wants to find, "
            f"compare or check the price of a product on {names} (e.g. 'znajdź na allegro najtańszy "
            "raspberry pi'), instead of web_search with 'site:'. Put only the product in `query`, "
            "without the shop name. For the cheapest offers use sort='price_asc' with a specific "
            "query (e.g. 'raspberry pi 4 4gb'): sorting by price also brings up cheap accessories. "
            "Prices are read from the shop's page at search time and may change; sponsored offers "
            "are marked [sponsored]."
        )
        self.input_schema = {
            "type": "object",
            "properties": {
                "site": {"type": "string", "enum": list(self._sites), "description": "The shop to search in."},
                "query": {
                    "type": "string",
                    "description": "What to look for, e.g. 'raspberry pi 5 8gb'.",
                },
                "max_results": {
                    "type": "integer",
                    "minimum": 1,
                    "maximum": MAX_RESULTS_LIMIT,
                    "description": f"How many offers to return, 1-{MAX_RESULTS_LIMIT}. Defaults to {DEFAULT_MAX_RESULTS}.",
                },
                "sort": {
                    "type": "string",
                    "enum": list(SORT_ORDERS),
                    "description": "Order of offers: the shop's relevance (default), or by price.",
                },
            },
            "required": ["site", "query"],
            "additionalProperties": False,
        }

    def _parse(self, input: dict) -> SiteSearchRequest:
        request = super()._parse(input)
        if request.site not in self._sites:
            raise ToolInputError(f"site must be one of: {', '.join(self._sites)}")
        return request

    def _heading(self, request: SiteSearchRequest, response: SearchResponse) -> str:
        return f'Offers on {request.site} for "{response.query}":'

    def _source(self, request: SiteSearchRequest) -> str:
        return f"site_search:{request.site}"

    def _notes(self, request: SiteSearchRequest) -> Sequence[str]:
        return () if request.sort == "relevance" else (PRICE_SORT_NOTE,)

    def _no_results(self, request: SiteSearchRequest, response: SearchResponse) -> str:
        return f'No offers on {request.site} for "{response.query}".'

    def _blocked(self, request: SiteSearchRequest, response: SearchResponse) -> str:
        return (
            f"{request.site} asks for human verification ({response.error}). Do not retry yourself: ask "
            f"the user to solve the check on the site browser's screen at {self._view_url} and then "
            "repeat the request."
        )

    def _failed(self, request: SiteSearchRequest, response: SearchResponse) -> str:
        return (
            f"Site search on {request.site} failed: {response.error or 'unknown error'}. If it keeps "
            "failing, ask the user to restart the site-agent container (`podman-compose restart site-agent`)."
        )
