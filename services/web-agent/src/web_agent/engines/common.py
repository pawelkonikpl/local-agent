"""Pieces every HTML-results engine shares: the DOM extraction script and turning raw entries
into clean, deduplicated `SearchResult`s."""

import json
import re
from collections.abc import Callable
from urllib.parse import urldefrag, urlparse, urlunparse

from web_agent.guard import domain_signals, injection_signals, sanitize
from web_agent.models import SearchResult

SNIPPET_MAX_CHARS = 300
WITHHELD_URL_MAX_CHARS = 200
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
    """Drop ads and non-http(s) links, deduplicate by URL without `#fragment`, rank from 1.

    Every text first goes through `guard.sanitize`. An entry whose title or snippet looks like
    instructions aimed at the model is kept but withheld: empty title and snippet, URL without its
    query string, so the model can name the page without reading what it says.
    """
    results: list[SearchResult] = []
    seen: set[str] = set()
    for entry in raw:
        if len(results) >= max_results:
            break
        if entry.get("is_ad"):
            continue
        href = sanitize(str(entry.get("href") or ""))
        url = resolve_href(href.text)
        title = sanitize(str(entry.get("title") or ""))
        snippet = sanitize(str(entry.get("snippet") or ""))
        if url is None or not _normalize(title.text):
            continue
        key = urldefrag(url).url
        if key in seen:
            continue
        seen.add(key)
        hostname = urlparse(url).hostname or ""
        # Signals are computed on the full text, before the snippet is cut short.
        flags = injection_signals(f"{title.text}\n{snippet.text}")
        if href.removed_tags or title.removed_tags or snippet.removed_tags:
            flags.append("hidden_unicode")
        withheld = bool(flags)
        flags += domain_signals(url)
        results.append(
            SearchResult(
                rank=len(results) + 1,
                title="" if withheld else _normalize(title.text),
                url=_without_query(url) if withheld else url,
                snippet="" if withheld else _normalize(snippet.text)[:SNIPPET_MAX_CHARS],
                domain=hostname.removeprefix("www."),
                flags=flags,
                withheld=withheld,
            )
        )
    return results


def _without_query(url: str) -> str:
    """`url` without query string and fragment, which could carry the attack text itself."""
    return urlunparse(urlparse(url)._replace(query="", fragment="", params=""))[:WITHHELD_URL_MAX_CHARS]


def _normalize(text: str) -> str:
    return _WHITESPACE.sub(" ", text).strip()
