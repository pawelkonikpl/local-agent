from urllib.parse import parse_qs, urlencode, urljoin, urlparse

from web_agent.engines.common import blocking_status, build_results, extract_js, http_url
from web_agent.models import SearchQuery, SearchResult

# The JS-free HTML frontend: stable server-rendered markup, no client-side rendering to wait for.
BASE_URL = "https://html.duckduckgo.com/html/"

# Selectors are the most fragile part of this engine; keep them all here. When DDG changes its
# markup, `parse` gets an empty list without a "no results" marker, and the search reports
# "unexpected page layout" instead of pretending nothing was found.
RESULT_SELECTOR = ".result"
TITLE_SELECTOR = ".result__a"
SNIPPET_SELECTOR = ".result__snippet"
AD_CLASS = "result--ad"

NO_RESULTS_MARKERS = ("No results.", "No  results.", "No results found")
BLOCK_MARKERS = (
    "bots use DuckDuckGo too",
    "confirm this search was made by a human",
)


def _resolve_href(href: str) -> str | None:
    """The target URL behind a result link, or None if it isn't a plain http(s) link.

    Organic results link through DDG's redirector (`//duckduckgo.com/l/?uddg=<target>`); anything
    else still pointing at duckduckgo.com (ads, `y.js` trackers) is dropped.
    """
    url = urljoin("https:", href.strip())
    parsed = urlparse(url)
    host = parsed.hostname or ""
    if host == "duckduckgo.com" or host.endswith(".duckduckgo.com"):
        if parsed.path != "/l/":
            return None
        targets = parse_qs(parsed.query).get("uddg")
        if not targets:
            return None
        url = targets[0]
    return http_url(url)


class DuckDuckGoEngine:
    name = "duckduckgo"
    extract_js = extract_js(
        result=RESULT_SELECTOR, title=TITLE_SELECTOR, snippet=SNIPPET_SELECTOR, ad_class=AD_CLASS
    )

    def url_for(self, query: SearchQuery, region: str) -> str:
        return f"{BASE_URL}?{urlencode({'q': query.query, 'kl': region})}"

    def parse(self, raw: list[dict], max_results: int) -> list[SearchResult]:
        return build_results(raw, max_results, _resolve_href)

    def detect_block(self, status: int | None, text: str) -> str | None:
        if blocking_status(status):
            return f"DuckDuckGo answered HTTP {status}"
        if any(marker in text for marker in BLOCK_MARKERS):
            return "DuckDuckGo served its bot challenge instead of results"
        return None

    def is_no_results(self, text: str) -> bool:
        return any(marker in text for marker in NO_RESULTS_MARKERS)
