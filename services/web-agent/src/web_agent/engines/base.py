from typing import Protocol

from web_agent.models import SearchQuery, SearchResult


class SearchEngine(Protocol):
    """One search engine's page layout: where to go, what to read, how to tell a block apart.

    Pure data and functions -- no browser here, so every engine is testable on saved pages. The
    browser work (navigate, evaluate, verify) lives in `search.SearchService`.
    """

    name: str
    # Origins besides the results page's own that the page needs to render (e.g. a script CDN).
    # The browser refuses every other origin; only code sets this, never a page or the model.
    extra_origins: frozenset[str]
    # JS expression evaluated in the results page; returns `[{title, href, snippet, is_ad}]`,
    # plus `price` for shops (see `models.RawEntry`).
    extract_js: str
    # How long a bot check may take to clear itself -- a JS check that reloads the page once the
    # browser passes it, as for any visitor -- before the search reports `blocked`. 0: don't wait.
    block_settle_s: float

    def url_for(self, query: SearchQuery, region: str) -> str: ...

    def parse(self, raw: list[dict], max_results: int) -> list[SearchResult]: ...

    def detect_block(self, status: int | None, text: str) -> str | None:
        """The reason the engine refused to serve results, or None if the page looks normal."""
        ...

    def is_no_results(self, text: str) -> bool: ...
