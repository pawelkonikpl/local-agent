import re

import httpx

from api.tools.base import Capability, ToolInputError, ToolResult
from api.tools.web_common import WITHHELD_NOTE, format_result, spotlight

DEFAULT_MAX_RESULTS = 5
MAX_RESULTS_LIMIT = 10
QUERY_MAX_CHARS = 400
REGION_PATTERN = re.compile(r"^[a-z]{2}-[a-z]{2}$")


class WebSearchTool:
    """`web_search`: web search results (title, URL, snippet) from the web-agent service.

    The browser runs in web-agent, a separate container with internet access and no route to
    `db`/`llm-proxy`; this tool only forwards the query there over HTTP.
    """

    name = "web_search"
    description = (
        "Searches the web and returns a numbered list of results: title, URL and a short snippet "
        "(not the full page contents). Use it whenever the answer depends on current events, "
        "recent releases or facts you are not sure about, e.g. query 'python 3.13 release notes'."
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
    ) -> None:
        self._base_url = base_url
        self._headers = {"Authorization": f"Bearer {token}"}
        self._timeout_s = timeout_s
        self._transport = transport

    async def run(self, input: dict) -> ToolResult:
        payload = self._validate(input)
        async with httpx.AsyncClient(
            base_url=self._base_url, headers=self._headers, timeout=self._timeout_s, transport=self._transport
        ) as client:
            try:
                response = await client.post("/v1/search", json=payload)
            except httpx.TimeoutException:
                return ToolResult(f"Web search timed out after {self._timeout_s:g}s.", is_error=True)
            except httpx.HTTPError as exc:
                return ToolResult(f"Web search service is unreachable: {type(exc).__name__}.", is_error=True)
        if response.status_code == 422:
            raise ToolInputError(f"web-agent rejected the input: {response.text[:300]}")
        if response.status_code != 200:
            return ToolResult(f"Web search service failed with HTTP {response.status_code}.", is_error=True)
        return _format(response.json())

    @staticmethod
    def _validate(input: dict) -> dict:
        query = input.get("query")
        if not isinstance(query, str) or not query.strip():
            raise ToolInputError("query must be a non-empty string")
        if len(query.strip()) > QUERY_MAX_CHARS:
            raise ToolInputError(f"query must be at most {QUERY_MAX_CHARS} characters")
        max_results = input.get("max_results", DEFAULT_MAX_RESULTS)
        if isinstance(max_results, bool) or not isinstance(max_results, int) or not 1 <= max_results <= MAX_RESULTS_LIMIT:
            raise ToolInputError(f"max_results must be an integer from 1 to {MAX_RESULTS_LIMIT}")
        payload = {"query": query.strip(), "max_results": max_results}
        region = input.get("region")
        if region is not None:
            if not isinstance(region, str) or not REGION_PATTERN.match(region):
                raise ToolInputError("region must look like pl-pl, us-en or wt-wt")
            payload["region"] = region
        return payload


def _format(body: dict) -> ToolResult:
    status = body.get("status")
    query = body.get("query", "")
    if status == "ok":
        results = body.get("results", [])
        lines = [f'Web results for "{query}":']
        for result in results:
            lines.extend(format_result(result))
        if any(result.get("withheld") for result in results):
            lines.append(WITHHELD_NOTE)
        return ToolResult(spotlight("\n".join(lines), source="web_search"))
    if status == "no_results":
        return ToolResult(f'No web results for "{query}".')
    if status == "blocked":
        return ToolResult(
            f"The search engine refused the request ({body.get('error')}). Retrying now won't help; "
            "answer without web results and tell the user the search was blocked.",
            is_error=True,
        )
    return ToolResult(f"Web search failed: {body.get('error') or 'unknown error'}.", is_error=True)
