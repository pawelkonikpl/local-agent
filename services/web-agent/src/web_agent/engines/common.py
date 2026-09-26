"""Pieces every HTML-results engine shares: the DOM extraction script and turning raw entries
into clean, deduplicated `SearchResult`s."""

import json
import re
from collections.abc import Callable
from urllib.parse import urldefrag, urlparse

from web_agent.models import SearchResult

SNIPPET_MAX_CHARS = 300
BLOCKING_STATUSES = frozenset({403, 429})

_WHITESPACE = re.compile(r"\s+")


def extract_js(*, result: str, title: str, snippet: str, ad_class: str | None = None) -> str:
    """JS expression returning `[{title, href, snippet, is_ad}]` for every `result` element."""
    is_ad = f"el.classList.contains({json.dumps(ad_class)})" if ad_class else "false"
    return f"""
        Array.from(document.querySelectorAll({json.dumps(result)})).map((el) => {{
            const link = el.querySelector({json.dumps(title)});
            const snippet = el.querySelector({json.dumps(snippet)});
            return {{
                title: link ? link.innerText : "",
                href: link ? link.getAttribute("href") || "" : "",
                snippet: snippet ? snippet.innerText : "",
                is_ad: {is_ad},
            }};
        }})
    """


def http_url(url: str) -> str | None:
    """`url` if it's an absolute http(s) URL, else None."""
    parsed = urlparse(url.strip())
    if parsed.scheme not in ("http", "https") or not parsed.hostname:
        return None
    return url.strip()


def blocking_status(status: int | None) -> bool:
    return status is not None and (status in BLOCKING_STATUSES or status >= 500)


def build_results(
    raw: list[dict], max_results: int, resolve_href: Callable[[str], str | None]
) -> list[SearchResult]:
    """Drop ads and non-http(s) links, deduplicate by URL without `#fragment`, rank from 1."""
    results: list[SearchResult] = []
    seen: set[str] = set()
    for entry in raw:
        if len(results) >= max_results:
            break
        if entry.get("is_ad"):
            continue
        url = resolve_href(str(entry.get("href") or ""))
        title = _normalize(str(entry.get("title") or ""))
        if url is None or not title:
            continue
        key = urldefrag(url).url
        if key in seen:
            continue
        seen.add(key)
        hostname = urlparse(url).hostname or ""
        results.append(
            SearchResult(
                rank=len(results) + 1,
                title=title,
                url=url,
                snippet=_normalize(str(entry.get("snippet") or ""))[:SNIPPET_MAX_CHARS],
                domain=hostname.removeprefix("www."),
            )
        )
    return results


def _normalize(text: str) -> str:
    return _WHITESPACE.sub(" ", text).strip()
