import re
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

REGION_PATTERN = re.compile(r"^[a-z]{2}-[a-z]{2}$")


class WebSearchTool:
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
    # The query leaves for public search engines; the results are page text.
    capabilities: frozenset[Capability] = frozenset({"reads_untrusted", "external_effect"})

    def __init__(
        self,
        *,
        base_url: str,
        token: str,
        timeout_s: float,
        transport: httpx.AsyncBaseTransport | None = None,
        site_search_sites: Sequence[str] = (),
    ) -> None:
        self._base_url = base_url
        self._token = token
        self._timeout_s = timeout_s
        self._transport = transport
        if site_search_sites:
            # Otherwise small models reach for web_search with `site:` out of habit.
            self.description = (
                f"{type(self).description} To find products or prices on {', '.join(site_search_sites)}, "
                "use site_search instead."
            )

    async def run(self, input: dict) -> ToolResult:
        payload = self._validate(input)
        response = await call_web_agent(
            base_url=self._base_url,
            token=self._token,
            timeout_s=self._timeout_s,
            transport=self._transport,
            path="/v1/search",
            payload=payload,
            label="Web search",
            unreachable_message="Web search service is unreachable",
        )
        return response if isinstance(response, ToolResult) else _format(response)

    @staticmethod
    def _validate(input: dict) -> dict:
        payload: dict = {"query": validate_query(input), "max_results": validate_max_results(input)}
        region = input.get("region")
        if region is not None:
            if not isinstance(region, str) or not REGION_PATTERN.match(region):
                raise ToolInputError("region must look like pl-pl, us-en or wt-wt")
            payload["region"] = region
        return payload


def _format(body: WebAgentResponse) -> ToolResult:
    if body.status == "ok":
        lines = [f'Web results for "{body.query}":']
        for result in body.results:
            lines.extend(format_result(result))
        if any(result.withheld for result in body.results):
            lines.append(WITHHELD_NOTE)
        return ToolResult(spotlight("\n".join(lines), source="web_search"))
    if body.status == "no_results":
        return ToolResult(f'No web results for "{body.query}".')
    if body.status == "blocked":
        return ToolResult(
            f"The search engine refused the request ({body.error}). Retrying now won't help; "
            "answer without web results and tell the user the search was blocked.",
            is_error=True,
        )
    return ToolResult(f"Web search failed: {body.error or 'unknown error'}.", is_error=True)
