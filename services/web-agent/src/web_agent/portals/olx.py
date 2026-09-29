"""olx.pl: plots for sale ("Działki - Sprzedaż") in one locality.

Checked on 28.09.2026 in a headless Chromium (listing with filters, offer). Plain HTTP clients get
CloudFront's 403 ("The request could not be satisfied"); a browser passes.
- The portal's autocomplete, `/api/v1/geo-encoder/location-autocomplete/?query=<name>`, answers
  `{data: [{city: {name, normalized_name}, municipality: {name}, county: {name}}]}`. Homonyms carry
  an id suffix in `normalized_name` (`gronowo_27659`).
- The listing, `/nieruchomosci/dzialki/sprzedaz/<normalized_name>/?search[filter_float_price:to]=
  &search[filter_float_m:from]=&search[order]=created_at:desc|filter_float_price:asc`. The first
  `[data-testid=listing-grid]` holds the matches; a second one, "Sprawdź ogłoszenia w większej
  odległości", offers from farther away (links tagged `reason=extended_search...`): never read.
- Cards (`[data-cy=l-card]`) carry title, price, "<place> - <date>" and "<area> - <price per m²>".
  Some link to otodom.pl (same group): those are kept, with their otodom.pl URL. Promoted cards
  carry `[data-testid=adCard-featured]` (PROVISIONAL: none in the checked listing).
- An offer page: `[data-testid=offer_title]`, `ad-price-container`, `ad-parameters-container`
  ("Prywatne"/"Firmowe", then "Rodzaj: Działki budowlane", "Powierzchnia: 670 m²"...),
  `ad-posted-at`; the seller card, phone and description are never read.
"""

import json
from urllib.parse import quote, urlencode, urljoin

from contracts.listings import offer_url
from pydantic import BaseModel, ValidationError

from web_agent.models import ListingSearchRequest, ListingSort
from web_agent.portals.base import MarkerPortal
from web_agent.portals.common import LocationMatch, LocationMiss, pick_location

BASE_URL = "https://www.olx.pl"
SORT_PARAMS: dict[ListingSort, str] = {"newest": "created_at:desc", "price_asc": "filter_float_price:asc"}
# otodom.pl offers OLX lists among its own.
PARTNER_PORTAL = "otodom.pl"

GRID_SELECTOR = '[data-testid="listing-grid"]'
CARD_SELECTOR = '[data-cy="l-card"]'
EXCLUDED_CONTAINERS = (
    '[data-testid="seller_card"]',
    '[data-testid="ad_description"]',
    '[data-testid="qa-advert-slot"]',
    '[class*="similar"]',
)

EXTRACT_JS = f"""
(() => {{
    const EXCLUDED = {json.dumps(", ".join(EXCLUDED_CONTAINERS))};
    const excluded = (el) => el.closest(EXCLUDED) !== null;
    const text = (root, selector) => {{
        const el = root.querySelector(selector);
        return el && !excluded(el) ? el.innerText : null;
    }};
    const grid = document.querySelector({json.dumps(GRID_SELECTOR)});
    const cards = grid ? Array.from(grid.querySelectorAll({json.dumps(CARD_SELECTOR)})) : [];
    const entries = cards.map((card) => {{
        const link = card.querySelector('[data-testid="ad-card-title"] a') || card.querySelector("a");
        const href = link ? link.getAttribute("href") || "" : "";
        if (!link || excluded(card) || href.includes("extended_search")) return null;
        // "Białka Tatrzańska - 25 sierpnia 2026", "4 252 m² - 4259.17 zł/m²"
        const [place, posted] = (text(card, '[data-testid="location-date"]') || "").split(" - ");
        const [area, perM2] = (text(card, '[data-testid="location-date"] ~ div span') ||
            text(card, '[data-nx-name="P5"]') || "").split(" - ");
        return {{
            title: text(card, '[data-testid="ad-card-title"] h4') || link.innerText,
            href,
            location: place || null,
            price: text(card, '[data-testid="ad-price"]'),
            area: area || null,
            price_per_m2: perM2 || null,
            listed: posted || null,
            sponsored: card.querySelector('[data-testid="adCard-featured"]') !== null,
        }};
    }}).filter((entry) => entry !== null);
    const crumbs = Array.from(document.querySelectorAll('[data-testid="breadcrumb-item"]'));
    return {{
        entries,
        heading: crumbs.length ? crumbs[crumbs.length - 1].innerText : null,
        count: text(document, '[data-testid="listing-count-msg"]'),
        no_results: false,
    }};
}})()
"""

DETAILS_JS = f"""
(() => {{
    const EXCLUDED = {json.dumps(", ".join(EXCLUDED_CONTAINERS))};
    const excluded = (el) => el.closest(EXCLUDED) !== null;
    const text = (selector) => {{
        const el = document.querySelector(selector);
        return el && !excluded(el) ? el.innerText : null;
    }};
    const title = text('[data-testid="offer_title"]');
    if (!title) return {{offer: null, params: []}};
    const params = [];
    let seller = null;
    for (const row of document.querySelectorAll('[data-testid="ad-parameters-container"] p')) {{
        if (excluded(row)) continue;
        const line = row.innerText.trim();
        const colon = line.indexOf(": ");
        if (colon < 0) seller = seller || line;  // "Prywatne" / "Firmowe"
        else params.push({{name: line.slice(0, colon), value: line.slice(colon + 2)}});
    }}
    const value = (name) => (params.find((p) => p.name === name) || {{}}).value || null;
    const crumbs = Array.from(document.querySelectorAll('[data-testid="breadcrumb-item"]'));
    const place = crumbs.length ? crumbs[crumbs.length - 1].innerText.replace(/^Sprzedaż - /, "") : null;
    return {{
        offer: {{
            title,
            href: location.href,
            location: place,
            price: text('[data-testid="ad-price-container"]'),
            area: value("Powierzchnia"),
            price_per_m2: value("Cena za m²"),
            plot_type: value("Rodzaj"),
            listed: text('[data-testid="ad-posted-at"]'),
            seller,
        }},
        params,
    }};
}})()
"""

BLOCK_MARKERS = ("The request could not be satisfied",)
# PROVISIONAL: to be confirmed on a saved page of an ended offer.
INACTIVE_MARKERS = ("To ogłoszenie nie jest już dostępne", "Ogłoszenie jest nieaktywne")


class _Named(BaseModel):
    name: str


class _City(BaseModel):
    name: str
    normalized_name: str


class _Place(BaseModel):
    city: _City
    municipality: _Named | None = None
    county: _Named | None = None


class _Autocomplete(BaseModel):
    data: list[dict] = []


class OlxPortal(MarkerPortal):
    name = "olx.pl"
    base_url = BASE_URL
    hosts = frozenset({"www.olx.pl"})
    extra_origins = frozenset()
    currency = "PLN"
    ready_js = f"document.querySelector({json.dumps(GRID_SELECTOR)}) !== null"
    extract_js = EXTRACT_JS
    details_js = DETAILS_JS
    block_markers = BLOCK_MARKERS
    inactive_markers = INACTIVE_MARKERS
    status_block_reason = "olx.pl bot protection (HTTP {status})"
    marker_block_reason = "olx.pl bot protection"

    def lookup_url(self, location: str) -> str:
        return f"{BASE_URL}/api/v1/geo-encoder/location-autocomplete/?{urlencode({'query': location})}"

    def resolve_location(self, lookup: object, location: str) -> LocationMatch | LocationMiss | None:
        try:
            answer = _Autocomplete.model_validate(lookup)
        except ValidationError:
            return None
        places: list[_Place] = []
        for item in answer.data:
            try:
                place = _Place.model_validate(item)
            except ValidationError:
                continue
            # The slug goes into the URL path: letters, digits, "-" and the "_<id>" suffix only.
            if place.city.normalized_name.replace("_", "-").replace("-", "").isalnum():
                places.append(place)
        match = pick_location(places, location, name=lambda place: place.city.name, label=_label)
        if isinstance(match, LocationMiss):
            return match
        return LocationMatch(key=match.city.normalized_name, name=_label(match), place=match.city.name)

    def url_for(self, request: ListingSearchRequest, location: LocationMatch) -> str:
        params: dict[str, str | int] = {}
        if request.max_price:
            params["search[filter_float_price:to]"] = request.max_price
        if request.min_area_m2:
            params["search[filter_float_m:from]"] = request.min_area_m2
        if sort := SORT_PARAMS.get(request.sort):
            params["search[order]"] = sort
        url = f"{BASE_URL}/nieruchomosci/dzialki/sprzedaz/{quote(location.key)}/"
        return f"{url}?{urlencode(params)}" if params else url

    def listing_url(self, href: str) -> str | None:
        url = urljoin(f"{BASE_URL}/", href.strip())
        return self.offer_url(url) or offer_url(PARTNER_PORTAL, url)

    def is_no_results(self, text: str) -> bool:
        return False


def _label(place: _Place) -> str:
    where = ", ".join(part.name for part in (place.municipality, place.county) if part)
    return f"{place.city.name} ({where})" if where else place.city.name
