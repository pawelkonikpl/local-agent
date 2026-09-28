from urllib.parse import urlencode

from web_agent.engines.common import blocking_status, build_results, extract_js, http_url
from web_agent.models import SearchQuery, SearchResult

# SearXNG's default ("simple") theme. Same rule as for DuckDuckGo: all selectors live here.
RESULT_SELECTOR = "#urls article.result"
TITLE_SELECTOR = "h3 a"
SNIPPET_SELECTOR = "p.content"

# Also what SearXNG shows when every upstream engine failed; those failures are only listed in
# its sidebar, per engine, so they end up as `no_results` here.
NO_RESULTS_MARKERS = ("No results were found",)

NO_REGION = "wt-wt"


def language_for(region: str) -> str:
    """DuckDuckGo-style `<country>-<language>` region (pl-pl, us-en) -> SearXNG language (pl-PL,
    en-US); `wt-wt` -> `all`."""
    if region == NO_REGION:
        return "all"
    country, _, language = region.partition("-")
    return f"{language}-{country.upper()}"


class SearxngEngine:
    """A self-hosted SearXNG instance: a metasearch engine that queries the public engines itself,
    so the browser here only ever talks to our own server."""

    name = "searxng"
    extra_origins: frozenset[str] = frozenset()
    block_settle_s = 0.0
    extract_js = extract_js(result=RESULT_SELECTOR, title=TITLE_SELECTOR, snippet=SNIPPET_SELECTOR)

    def __init__(self, base_url: str) -> None:
        self._base_url = base_url.rstrip("/")

    def url_for(self, query: SearchQuery, region: str) -> str:
        params = {"q": query.query, "language": language_for(region), "categories": "general"}
        return f"{self._base_url}/search?{urlencode(params)}"

    def parse(self, raw: list[dict], max_results: int) -> list[SearchResult]:
        return build_results(raw, max_results, http_url)

    def detect_block(self, status: int | None, text: str) -> str | None:
        if blocking_status(status):
            return f"SearXNG answered HTTP {status}"
        return None

    def is_no_results(self, text: str) -> bool:
        return any(marker in text for marker in NO_RESULTS_MARKERS)
