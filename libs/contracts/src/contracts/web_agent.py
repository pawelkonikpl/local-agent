"""The web-agent API as api's web tools call it.

`POST /v1/search` takes `WebSearchRequest`, `POST /v1/site-search` takes `SiteSearchRequest`, and
both answer `SearchResponse`: a blocked or failed search is still HTTP 200, its outcome in `status`.
`GET /v1/sites` answers `SitesResponse`. All need the internal bearer token (`contracts.auth`).
"""

from typing import Annotated, Literal

from pydantic import BaseModel, Field, StringConstraints

SEARCH_PATH = "/v1/search"
SITE_SEARCH_PATH = "/v1/site-search"
SITES_PATH = "/v1/sites"

DEFAULT_MAX_RESULTS = 5
MAX_RESULTS_LIMIT = 10
QUERY_MAX_CHARS = 400
# DuckDuckGo-style region code, e.g. "pl-pl", "us-en", or "wt-wt" for no region.
REGION_PATTERN = r"^[a-z]{2}-[a-z]{2}$"

# Result order; only sites (shops) honor it, web search engines ignore it.
SortOrder = Literal["relevance", "price_asc", "price_desc"]
SearchStatus = Literal["ok", "no_results", "blocked", "error"]

# `SearchResult.flags`: a sponsored offer (shops only), and the prompt-injection signals that get a
# result withheld. Domain warnings come as `<kind>:<detail>`, e.g. `lookalike_domain:paypal.com`.
SPONSORED_FLAG = "sponsored"
HIDDEN_UNICODE_FLAG = "hidden_unicode"
INJECTION_FLAGS = frozenset(
    {
        "instruction_override",
        "fake_system",
        "ai_addressed",
        "fake_approval",
        "payment_demand",
        "credential_request",
        HIDDEN_UNICODE_FLAG,
    }
)

Query = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=QUERY_MAX_CHARS)]
Region = Annotated[str, StringConstraints(pattern=REGION_PATTERN)]


class SearchRequest(BaseModel):
    query: Query
    max_results: int = Field(default=DEFAULT_MAX_RESULTS, ge=1, le=MAX_RESULTS_LIMIT)


class WebSearchRequest(SearchRequest):
    region: Region | None = None


class SiteSearchRequest(SearchRequest):
    """A search inside one site's own search page; `site` is a name from `GET /v1/sites`."""

    site: str
    sort: SortOrder = "relevance"


class SearchResult(BaseModel):
    rank: int
    title: str
    url: str
    snippet: str
    domain: str
    # Price text as the page shows it, whitespace normalized (e.g. "1 299,00 zł"); shops only.
    price: str | None = None
    # Why this result deserves caution (`instruction_override`, `lookalike_domain:paypal.com`, ...).
    flags: list[str] = []
    # Title and snippet dropped as suspected prompt injection; only the URL is kept, to report it.
    withheld: bool = False


class SearchResponse(BaseModel):
    status: SearchStatus
    engine: str
    query: str
    results: list[SearchResult] = []
    error: str | None = None
    elapsed_ms: int


class SitesResponse(BaseModel):
    sites: list[str]
