from collections.abc import Sequence

from contracts.web_agent import (
    DEFAULT_MAX_RESULTS,
    MAX_RESULTS_LIMIT,
    SEARCH_PATH,
    SearchResponse,
    WebSearchRequest,
)

from api.tools.web_agent import SearchTool, WebAgentClient


class WebSearchTool(SearchTool[WebSearchRequest]):
    """`web_search`: web search results (title, URL, snippet) from the web-agent service.

    The browser runs in web-agent, a separate container with internet access and no route to
    `db`/`llm-proxy`; this tool only forwards the query there over HTTP.
    """

    name = "web_search"
    # Small models decide whether to search almost entirely from this text, and
    # tend to answer price/shopping questions from memory unless those are named explicitly.
    description = (
        "Searches the web and returns a numbered list of results: title, URL and a short snippet "
        "(not the full page contents). This is your access to the internet. Call it whenever "
        "the user asks you to search, look something up online or check a website (e.g. "
        "Ceneo, Wikipedia), and whenever the answer depends on current or changing information: "
        "prices, where to buy something, product availability, weather, news, election or sports "
        "results, recent releases, or facts you are not sure about. Search instead of guessing or "
        "saying you cannot browse. Not needed for writing, math or explaining general concepts. "
        "Example queries: 'miedziana patelnia cena', 'pogoda Gdańsk jutro', "
        "'python 3.13 release notes'."
    )
    input_schema = {
        "type": "object",
        "properties": {
            "query": {
                "type": "string",
                "description": "What to search for, e.g. 'podman rootless networking'.",
            },
            "max_results": {
                "type": "integer",
                "minimum": 1,
                "maximum": MAX_RESULTS_LIMIT,
                "description": f"How many results to return, 1-{MAX_RESULTS_LIMIT}. Defaults to {DEFAULT_MAX_RESULTS}.",
            },
            "region": {
                "type": "string",
                "description": "Region code for localized results, e.g. pl-pl, us-en, or wt-wt for none.",
            },
        },
        "required": ["query"],
        "additionalProperties": False,
    }
    request_model = WebSearchRequest
    path = SEARCH_PATH
    label = "Web search"
    unreachable_message = "Web search service is unreachable"

    def __init__(self, client: WebAgentClient, *, site_search_sites: Sequence[str] = ()) -> None:
        super().__init__(client)
        if site_search_sites:
            # Otherwise small models reach for web_search with `site:` out of habit.
            self.description = (
                f"{type(self).description} To find products or prices on {', '.join(site_search_sites)}, "
                "use site_search instead."
            )

    def _heading(self, request: WebSearchRequest, response: SearchResponse) -> str:
        return f'Web results for "{response.query}":'

    def _source(self, request: WebSearchRequest) -> str:
        return "web_search"

    def _no_results(self, request: WebSearchRequest, response: SearchResponse) -> str:
        return f'No web results for "{response.query}".'

    def _blocked(self, request: WebSearchRequest, response: SearchResponse) -> str:
        return (
            f"The search engine refused the request ({response.error}). Retrying now won't help; "
            "answer without web results and tell the user the search was blocked."
        )

    def _failed(self, request: WebSearchRequest, response: SearchResponse) -> str:
        return f"Web search failed: {response.error or 'unknown error'}."
