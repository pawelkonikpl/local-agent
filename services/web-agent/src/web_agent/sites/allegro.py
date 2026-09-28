"""allegro.pl's own listing page, read in the headful Chrome of the `site-agent` container.

Only allowlisted fields are read from each offer tile: the title (text of the heading link),
the price, the URL, the "sponsored" label and the tile's structured parameters as the snippet.
Nothing else on the page -- reviews, comments, Q&A, descriptions, seller history, banners -- is
ever read, including sections Allegro adds later, because no field selector points at them. Offer
titles are written by sellers, so they still go through `sanitize` and the injection signals.

Selectors checked on a listing saved from headful Chrome (28.09.2026). Read from the DOM, not
from the page's embedded JSON: the DOM gives exactly the tile fields we allow, anchored on
semantics (`article`, `h2 a`, the price's `aria-label`) rather than generated class names.
"""

import json
from urllib.parse import parse_qs, urlencode, urljoin, urlparse

from web_agent.engines.common import blocking_status, build_results
from web_agent.models import SearchQuery, SearchResult, SortOrder

BASE_URL = "https://allegro.pl"
HOSTS = frozenset({"allegro.pl", "www.allegro.pl"})
OFFER_PATH_PREFIX = "/oferta/"
PRODUCT_PATH_PREFIX = "/produkt/"
# Sponsored offers link through a click tracker carrying the offer URL in `redirect`.
CLICK_TRACKER_PATH = "/events/clicks"
SNIPPET_MAX_CHARS = 150
# Allegro's `order` parameter; relevance is its default, sent as no parameter at all.
ORDER_PARAMS: dict[SortOrder, str] = {"price_asc": "p", "price_desc": "pd"}

# Selectors are the most fragile part of this site; keep them all here. When Allegro changes its
# markup, `parse` gets an empty list without a "no results" marker, and the search reports
# "unexpected page layout" instead of pretending nothing was found.
# One offer per `article`; its title is the heading link (links to /produkt/..., /oferta/... or,
# for sponsored offers, the click tracker; Allegro Lokalnie tiles link to allegrolokalnie.pl).
TILE_SELECTOR = "article"
TITLE_LINK_SELECTOR = "h2 a"
# e.g. aria-label="829,90 zł aktualna cena"
PRICE_LABEL_SELECTOR = '[aria-label$="aktualna cena"]'
# Structured parameters of the tile ("Waga produktu ... 0.065 kg"), as a definition list.
PARAMS_SELECTOR = "dl"
SPONSORED_LABELS = ("Sponsorowane", "Oferta sponsorowana")
# Second layer on top of the field allowlist: a tile whose fields sit inside any of these is
# skipped. None of these occur in the listing today; they guard against Allegro adding such sections.
EXCLUDED_CONTAINERS = (
    '[itemprop="review"]',
    '[data-role*="review"]',
    '[data-role*="comment"]',
    '[data-role*="question"]',
    '[id*="opinie"]',
    '[id*="komentarze"]',
    '[id*="pytania"]',
)

# DataDome's challenge page: HTTP 403 and only this text, the challenge itself sits in an iframe.
BLOCK_MARKERS = ("Please enable JS and disable any ad blocker",)
# PROVISIONAL, to be confirmed on a saved "no results" page.
NO_RESULTS_MARKERS = ("Nie znaleźliśmy ofert", "Brak wyników")

EXTRACT_JS = f"""
(() => {{
    const EXCLUDED = {json.dumps(", ".join(EXCLUDED_CONTAINERS))};
    const SPONSORED = {json.dumps(SPONSORED_LABELS)};
    const PRICE = /\\d[\\d\\s\\u00a0]*(,\\d{{2}})?\\s*zł/;
    const excluded = (el) => el.closest(EXCLUDED) !== null;
    const tiles = Array.from(document.querySelectorAll({json.dumps(TILE_SELECTOR)}))
        .filter((tile) => !excluded(tile));
    const priceOf = (tile) => {{
        const labelled = tile.querySelector({json.dumps(PRICE_LABEL_SELECTOR)});
        if (labelled && !excluded(labelled)) {{
            const match = (labelled.getAttribute("aria-label") || "").match(PRICE);
            if (match) return match[0];
        }}
        return null;
    }};
    const paramsOf = (tile) => Array.from(tile.querySelectorAll({json.dumps(PARAMS_SELECTOR)}))
        .filter((dl) => !excluded(dl))
        .map((dl) => dl.innerText)
        .join("; ")
        .slice(0, {SNIPPET_MAX_CHARS});
    const isSponsored = (tile) => Array.from(tile.querySelectorAll("span, div, p"))
        .some((el) => el.children.length === 0 && SPONSORED.includes(el.innerText.trim()));
    return tiles.map((tile) => {{
        const link = tile.querySelector({json.dumps(TITLE_LINK_SELECTOR)});
        if (!link || excluded(link)) return null;
        return {{
            title: link.innerText,
            href: link.getAttribute("href") || "",
            snippet: paramsOf(tile),
            price: priceOf(tile),
            is_ad: isSponsored(tile),
        }};
    }}).filter((entry) => entry !== null);
}})()
"""


def allegro_url(href: str, *, follow_tracker: bool = True) -> str | None:
    """The offer's canonical URL, `https://allegro.pl/oferta/<id-or-slug>`, with no query string or
    fragment; None for anything else, including links pointing outside allegro.pl.

    `/produkt/<slug>?offerId=<id>` becomes `/oferta/<id>` (without `offerId`, the product page
    itself); a sponsored offer's click tracker is followed once, to the offer in its `redirect`.
    """
    parsed = urlparse(urljoin(f"{BASE_URL}/", href.strip()))
    if parsed.scheme != "https" or parsed.hostname not in HOSTS:
        return None
    query = parse_qs(parsed.query)
    if parsed.path == CLICK_TRACKER_PATH and follow_tracker:
        targets = query.get("redirect")
        return allegro_url(targets[0], follow_tracker=False) if targets else None
    if parsed.path.startswith(PRODUCT_PATH_PREFIX) and parsed.path != PRODUCT_PATH_PREFIX:
        offer_ids = [value for value in query.get("offerId", []) if value.isdigit()]
        return f"{BASE_URL}{OFFER_PATH_PREFIX}{offer_ids[0]}" if offer_ids else f"{BASE_URL}{parsed.path}"
    if parsed.path.startswith(OFFER_PATH_PREFIX) and parsed.path != OFFER_PATH_PREFIX:
        return f"{BASE_URL}{parsed.path}"
    return None


class AllegroSite:
    name = "allegro.pl"
    # PROVISIONAL, to be confirmed in DevTools (Network: Script, Stylesheet, XHR/Fetch) on the real
    # listing. The captcha-delivery.com origins serve DataDome's challenge (seen in its 403 page):
    # allowed only so a person can solve it in the site browser window; its content never
    # reaches the model, `EXTRACT_JS` reads the listing only.
    extra_origins: frozenset[str] = frozenset(
        {
            "https://assets.allegrostatic.com:443",
            "https://geo.captcha-delivery.com:443",
            "https://ct.captcha-delivery.com:443",
        }
    )
    extract_js = EXTRACT_JS
    # DataDome first serves a fresh profile a 403 page running an invisible device check
    # (`ct.captcha-delivery.com/i.js`), which reloads the page once passed.
    block_settle_s = 10.0

    def url_for(self, query: SearchQuery, region: str | None = None) -> str:
        """The listing URL; Allegro is Polish-only, so `region` is ignored."""
        params = {"string": query.query}
        if order := ORDER_PARAMS.get(query.sort):
            params["order"] = order
        return f"{BASE_URL}/listing?{urlencode(params)}"

    def parse(self, raw: list[dict], max_results: int) -> list[SearchResult]:
        return build_results(raw, max_results, allegro_url, ads="flag", snippet_max_chars=SNIPPET_MAX_CHARS)

    def detect_block(self, status: int | None, text: str) -> str | None:
        if blocking_status(status):
            return f"allegro.pl bot protection (HTTP {status})"
        if any(marker in text for marker in BLOCK_MARKERS):
            return "allegro.pl bot protection (captcha)"
        return None

    def is_no_results(self, text: str) -> bool:
        return any(marker in text for marker in NO_RESULTS_MARKERS)
