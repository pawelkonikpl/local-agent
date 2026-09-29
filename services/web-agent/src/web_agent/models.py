"""web-agent's own models. What it answers over HTTP (`SearchResponse`, `ListingSearchResponse` and
their parts) is the contract in `contracts.web_agent` and `contracts.listings`, re-exported here for
the engines and portals."""

from contracts.listings import (
    Currency,
    DetailsStatus,
    Listing,
    ListingDetailsRequest,
    ListingDetailsResponse,
    ListingParam,
    ListingSearchRequest,
    ListingSearchResponse,
    ListingSort,
    ListingStatus,
)
from contracts.web_agent import (
    Region,
    SearchRequest,
    SearchResponse,
    SearchResult,
    SearchStatus,
    SortOrder,
)
from pydantic import BaseModel

__all__ = [
    "Currency",
    "DetailsStatus",
    "Listing",
    "ListingDetailsRequest",
    "ListingDetailsResponse",
    "ListingParam",
    "ListingSearchRequest",
    "ListingSearchResponse",
    "ListingSort",
    "ListingStatus",
    "RawEntry",
    "SearchQuery",
    "SearchResponse",
    "SearchResult",
    "SearchStatus",
    "SortOrder",
]


class SearchQuery(SearchRequest):
    """What an engine searches for: a web search's region and a site search's sort, in one."""

    region: Region | None = None
    sort: SortOrder = "relevance"


class RawEntry(BaseModel):
    """One entry as an engine's `extract_js` returns it, before any cleaning."""

    title: str = ""
    href: str = ""
    snippet: str = ""
    price: str | None = None
    is_ad: bool = False
