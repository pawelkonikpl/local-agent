from collections.abc import Sequence

import httpx

from api.tools.base import Capability, ToolInputError, ToolResult
from api.tools.web_common import (
    DEFAULT_MAX_RESULTS,
    MAX_RESULTS_LIMIT,
    WITHHELD_NOTE,
    WebAgentResponse,
    call_web_agent,
    format_result,
    spotlight,
    validate_max_results,
    validate_query,
)

SORT_ORDERS = ("relevance", "price_asc", "price_desc")
# Shops pin sponsored and promoted offers above a price-sorted list.
PRICE_SORT_NOTE = (
    "Sorted by price, but the shop pins sponsored and promoted offers on top: compare the prices "
    "yourself and check that an offer is the product itself, not an accessory."
)
SITE_AGENT_DOWN = (
    "The shop search service (the site-agent container) isn't running; ask the user to start it "
    "with `podman-compose up -d site-agent`."
)


class SiteSearchTool:
    """`site_search`: offers from a shop's own search page (title, price, URL).

    Served by a separate web-agent instance (`site-agent`) driving a real, headful Chrome, because
    shops like Allegro block a headless browser. The model only names a site from a fixed list, a
    query and a sort order; web-agent builds the URL.
    """

    name = "site_search"
    # The query leaves for the shop; the results are page text written by sellers.
    capabilities: frozenset[Capability] = frozenset({"reads_untrusted", "external_effect"})

    def __init__(
        self,
        *,
        base_url: str,
        token: str,
        timeout_s: float,
        sites: Sequence[str],
        view_url: str,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        if not sites:
            raise ValueError("site_search needs at least one site")
        self._base_url = base_url
        self._token = token
        self._timeout_s = timeout_s
        self._sites = tuple(sites)
        self._view_url = view_url
        self._transport = transport
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

    async def run(self, input: dict) -> ToolResult:
        payload = self._validate(input)
        response = await call_web_agent(
            base_url=self._base_url,
            token=self._token,
            timeout_s=self._timeout_s,
            transport=self._transport,
            path="/v1/site-search",
            payload=payload,
            label="Site search",
            unreachable_message=SITE_AGENT_DOWN,
        )
        if isinstance(response, ToolResult):
            return response
        return _format(response, site=payload["site"], sort=payload["sort"], view_url=self._view_url)

    def _validate(self, input: dict) -> dict:
        site = input.get("site")
        if site not in self._sites:
            raise ToolInputError(f"site must be one of: {', '.join(self._sites)}")
        sort = input.get("sort", "relevance")
        if sort not in SORT_ORDERS:
            raise ToolInputError(f"sort must be one of: {', '.join(SORT_ORDERS)}")
        return {
            "site": site,
            "query": validate_query(input),
            "max_results": validate_max_results(input),
            "sort": sort,
        }


def _format(body: WebAgentResponse, *, site: str, sort: str, view_url: str) -> ToolResult:
    if body.status == "ok":
        lines = [f'Offers on {site} for "{body.query}":']
        for result in body.results:
            lines.extend(format_result(result))
        if any(result.withheld for result in body.results):
            lines.append(WITHHELD_NOTE)
        if sort != "relevance":
            lines.append(PRICE_SORT_NOTE)
        return ToolResult(spotlight("\n".join(lines), source=f"site_search:{site}"))
    if body.status == "no_results":
        return ToolResult(f'No offers on {site} for "{body.query}".')
    if body.status == "blocked":
        return ToolResult(
            f"{site} asks for human verification ({body.error}). Do not retry yourself: ask the user "
            f"to solve the check on the site browser's screen at {view_url} and then repeat the "
            "request.",
            is_error=True,
        )
    return ToolResult(
        f"Site search on {site} failed: {body.error or 'unknown error'}. If it keeps failing, ask the "
        "user to restart the site-agent container (`podman-compose restart site-agent`).",
        is_error=True,
    )
