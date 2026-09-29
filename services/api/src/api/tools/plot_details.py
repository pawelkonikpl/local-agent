from collections.abc import Sequence

from contracts.listings import (
    LISTING_DETAILS_PATH,
    ListingDetailsRequest,
    ListingDetailsResponse,
    offer_url,
)

from api.tools.base import ToolInputError, ToolResult
from api.tools.plot_search import listing_lines
from api.tools.site_search import SITE_AGENT_DOWN
from api.tools.web_agent import WITHHELD_NOTE, WebAgentClient, WebAgentTool, spotlight

DESCRIPTION_NOTE = "Description not read — ask the user to read it on the page before relying on it."


class PlotDetailsTool(WebAgentTool[ListingDetailsRequest, ListingDetailsResponse]):
    """`plot_details`: one listing page of a real-estate portal -- its facts and its parameter table.

    The URL is the only input that comes from the model: it must be an offer page of the named
    portal (`contracts.listings.offer_url`), checked here before any request and again in web-agent.
    """

    name = "plot_details"
    request_model = ListingDetailsRequest
    response_model = ListingDetailsResponse
    path = LISTING_DETAILS_PATH
    label = "Plot details"
    unreachable_message = SITE_AGENT_DOWN

    def __init__(self, client: WebAgentClient, *, portals: Sequence[str], view_url: str) -> None:
        if not portals:
            raise ValueError("plot_details needs at least one portal")
        super().__init__(client)
        self._portals = tuple(portals)
        self._view_url = view_url
        self.description = (
            "Opens one plot listing found by plot_search and returns its facts (price, area, price per "
            "m², plot type, dates, seller type) and the portal's parameter table (e.g. utilities, "
            "access road, shape, dimensions), or says the listing is no longer active. Use it to "
            "check a listing before recommending it. The listing's description is not read: ask the "
            "user to read it on the page."
        )
        self.input_schema = {
            "type": "object",
            "properties": {
                "portal": {
                    "type": "string",
                    "enum": list(self._portals),
                    "description": "The portal the URL belongs to (an olx.pl result may link to otodom.pl: then otodom.pl).",
                },
                "url": {"type": "string", "description": "A listing URL exactly as plot_search returned it."},
            },
            "required": ["portal", "url"],
            "additionalProperties": False,
        }

    def _parse(self, input: dict) -> ListingDetailsRequest:
        request = super()._parse(input)
        if request.portal not in self._portals:
            raise ToolInputError(f"portal must be one of: {', '.join(self._portals)}")
        url = offer_url(request.portal, request.url)
        if url is None:
            raise ToolInputError(
                f"url must be a listing page of {request.portal}, as returned by plot_search "
                "(an olx.pl result linking to otodom.pl needs portal otodom.pl)"
            )
        return request.model_copy(update={"url": url})

    def _format(self, request: ListingDetailsRequest, response: ListingDetailsResponse) -> ToolResult:
        match response.status:
            case "ok" if response.listing is not None:
                listing = response.listing
                lines = [f"Listing on {request.portal}:", *listing_lines(listing)]
                if listing.withheld:
                    lines.append(WITHHELD_NOTE)
                elif response.params:
                    lines.append("Parameters as the portal lists them:")
                    lines.extend(f"- {param.name}: {param.value}" for param in response.params)
                else:
                    lines.append("The portal lists no parameters for this listing.")
                lines.append(DESCRIPTION_NOTE)
                return ToolResult(spotlight("\n".join(lines), source=f"plot_details:{request.portal}"))
            case "inactive":
                return ToolResult(f"The listing {request.url} is no longer active on {request.portal} (removed or ended).")
            case "blocked":
                return ToolResult(
                    f"{request.portal} asks for human verification ({response.error}). Do not retry "
                    f"yourself: ask the user to solve the check on the site browser's screen at "
                    f"{self._view_url} and then repeat the request.",
                    is_error=True,
                )
            case _:
                return ToolResult(
                    f"Reading the listing on {request.portal} failed: {response.error or 'unknown error'}.",
                    is_error=True,
                )
