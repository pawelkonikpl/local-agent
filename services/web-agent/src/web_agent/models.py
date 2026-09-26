from typing import Annotated, Literal

from pydantic import BaseModel, Field, StringConstraints

REGION_PATTERN = r"^[a-z]{2}-[a-z]{2}$"


class SearchQuery(BaseModel):
    query: Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=400)]
    max_results: int = Field(default=5, ge=1, le=10)
    # DuckDuckGo-style region code, e.g. "pl-pl", "us-en", or "wt-wt" for no region.
    region: Annotated[str, StringConstraints(pattern=REGION_PATTERN)] | None = None


class SearchResult(BaseModel):
    rank: int
    title: str
    url: str
    snippet: str
    domain: str


SearchStatus = Literal["ok", "no_results", "blocked", "error"]


class SearchResponse(BaseModel):
    status: SearchStatus
    engine: str
    query: str
    results: list[SearchResult] = []
    error: str | None = None
    elapsed_ms: int
