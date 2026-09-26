from typing import Protocol

from web_agent.models import SearchQuery, SearchResult


class SearchEngine(Protocol):
    """One search engine's page layout: where to go, what to read, how to tell a block apart.

    Pure data and functions -- no browser here, so every engine is testable on saved pages. The
    browser work (navigate, evaluate, verify) lives in `search.SearchService`.
    """

    name: str
    # JS expression evaluated in the results page; returns `[{title, href, snippet, is_ad}]`.
    extract_js: str

    def url_for(self, query: SearchQuery, region: str) -> str: ...

    def parse(self, raw: list[dict], max_results: int) -> list[SearchResult]: ...

    def detect_block(self, status: int | None, text: str) -> str | None:
        """The reason the engine refused to serve results, or None if the page looks normal."""
        ...

    def is_no_results(self, text: str) -> bool: ...
