"""Pieces every HTML-results engine shares: the DOM extraction script, block and no-results
detection by page markers, screening one entry's texts (`screen`, also used by the portals), and
turning raw entries into clean, deduplicated `SearchResult`s."""

import json
import logging
import re
from collections.abc import Callable, Iterator
from dataclasses import dataclass
from typing import Literal
from urllib.parse import urldefrag, urlparse, urlunparse

from contracts.web_agent import HIDDEN_UNICODE_FLAG, SPONSORED_FLAG
from pydantic import ValidationError

from web_agent.guard import domain_signals, injection_signals, sanitize
from web_agent.models import RawEntry, SearchResult

logger = logging.getLogger(__name__)

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


class MarkerEngine:
    """The `SearchEngine` parts that recognize a page by status and text markers.

    A blocking HTTP status or any of `block_markers` in the page text is a block; any of
    `no_results_markers` is an honest "nothing found". Engines set the markers and reasons and
    add what's their own (`url_for`, `parse`, `extract_js`).
    """

    name: str
    extra_origins: frozenset[str] = frozenset()
    block_settle_s: float = 0.0
    block_markers: tuple[str, ...] = ()
    no_results_markers: tuple[str, ...] = ()
    # `{status}` is filled in; the model and the user read both reasons.
    status_block_reason: str
    marker_block_reason: str = ""

    def detect_block(self, status: int | None, text: str) -> str | None:
        if blocking_status(status):
            return self.status_block_reason.format(status=status)
        if any(marker in text for marker in self.block_markers):
            return self.marker_block_reason
        return None

    def is_no_results(self, text: str) -> bool:
        return any(marker in text for marker in self.no_results_markers)


def build_results(
    raw: list[dict],
    max_results: int,
    resolve_href: Callable[[str], str | None],
    *,
    ads: Literal["drop", "flag"] = "drop",
    snippet_max_chars: int = SNIPPET_MAX_CHARS,
) -> list[SearchResult]:
    """Drop non-http(s) links, deduplicate by URL without `#fragment`, rank from 1.

    Ads are dropped by default; with `ads="flag"` (shops, where sponsored offers are real offers)
    they stay, flagged `sponsored`. Every text first goes through `guard.sanitize`. An entry whose
    title, snippet or price looks like instructions aimed at the model is kept but withheld: empty
    title, snippet and price, URL without its query string, so the model can name the page without
    reading what it says.
    """
    results: list[SearchResult] = []
    seen: set[str] = set()
    for entry in _raw_entries(raw):
        if len(results) >= max_results:
            break
        if entry.is_ad and ads == "drop":
            continue
        href = sanitize(entry.href)
        url = resolve_href(href.text)
        screened = screen(title=entry.title, snippet=entry.snippet, price=entry.price or "")
        if url is None or not screened.texts["title"]:
            continue
        key = urldefrag(url).url
        if key in seen:
            continue
        seen.add(key)
        hostname = urlparse(url).hostname or ""
        flags = list(screened.flags)
        if href.removed_tags and HIDDEN_UNICODE_FLAG not in flags:
            flags.append(HIDDEN_UNICODE_FLAG)
        withheld = bool(flags)
        flags += domain_signals(url)
        if entry.is_ad:
            flags.append(SPONSORED_FLAG)
        results.append(
            SearchResult(
                rank=len(results) + 1,
                title="" if withheld else screened.texts["title"],
                url=without_query(url) if withheld else url,
                snippet="" if withheld else screened.texts["snippet"][:snippet_max_chars],
                domain=hostname.removeprefix("www."),
                price=None if withheld else (screened.texts["price"] or None),
                flags=flags,
                withheld=withheld,
            )
        )
    return results


@dataclass(frozen=True)
class Screened:
    """Texts of one entry after `screen`: sanitized and whitespace-normalized, by field name."""

    texts: dict[str, str]
    # Prompt-injection signals, plus `hidden_unicode` if any text carried Unicode tag characters.
    flags: tuple[str, ...]

    @property
    def withheld(self) -> bool:
        """An entry with any signal is kept but withheld: its texts must not reach the model."""
        return bool(self.flags)


def screen(**fields: str) -> Screened:
    """Every text of one entry through `guard.sanitize`, then the injection signals over all of them
    together. Signals are computed on the full texts, before any is cut short."""
    sanitized = {name: sanitize(text) for name, text in fields.items()}
    flags = injection_signals("\n".join(text.text for text in sanitized.values()))
    if any(text.removed_tags for text in sanitized.values()):
        flags.append(HIDDEN_UNICODE_FLAG)
    return Screened({name: _normalize(text.text) for name, text in sanitized.items()}, tuple(flags))


def _raw_entries(raw: list[dict]) -> Iterator[RawEntry]:
    """`raw` parsed into `RawEntry`s; entries of an unexpected shape are skipped, not fatal."""
    for item in raw:
        try:
            yield RawEntry.model_validate(item)
        except ValidationError:
            logger.debug("Skipping a malformed raw entry: %.200r", item)


def without_query(url: str) -> str:
    """`url` without query string and fragment, which could carry the attack text itself."""
    return urlunparse(urlparse(url)._replace(query="", fragment="", params=""))[:WITHHELD_URL_MAX_CHARS]


def _normalize(text: str) -> str:
    return _WHITESPACE.sub(" ", text).strip()
