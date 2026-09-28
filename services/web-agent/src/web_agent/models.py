from typing import Annotated, Literal

from pydantic import BaseModel, Field, StringConstraints

REGION_PATTERN = r"^[a-z]{2}-[a-z]{2}$"

# Result order; only sites (shops) honor it, web search engines ignore it.
SortOrder = Literal["relevance", "price_asc", "price_desc"]


class SearchQuery(BaseModel):
    query: Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=400)]
    max_results: int = Field(default=5, ge=1, le=10)
    # DuckDuckGo-style region code, e.g. "pl-pl", "us-en", or "wt-wt" for no region.
    region: Annotated[str, StringConstraints(pattern=REGION_PATTERN)] | None = None
    sort: SortOrder = "relevance"


class SiteSearchQuery(BaseModel):
    """A search inside one site's own search page; `site` is a name from `sites.SITE_NAMES`."""

    site: str
    query: Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=400)]
    max_results: int = Field(default=5, ge=1, le=10)
    sort: SortOrder = "relevance"


class RawEntry(BaseModel):
    """One entry as an engine's `extract_js` returns it, before any cleaning."""

    title: str = ""
    href: str = ""
    snippet: str = ""
    price: str | None = None
    is_ad: bool = False


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


SearchStatus = Literal["ok", "no_results", "blocked", "error"]


class SearchResponse(BaseModel):
    status: SearchStatus
    engine: str
    query: str
    results: list[SearchResult] = []
    error: str | None = None
    elapsed_ms: int
