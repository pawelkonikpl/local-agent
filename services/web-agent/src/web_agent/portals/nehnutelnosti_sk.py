"""nehnutelnosti.sk: plots ("pozemky") for sale in one Slovak municipality, prices in EUR.

Checked on 28.09.2026 in a headless Chromium (listing, filters, offer, removed offer):
- The portal's autocomplete, `/api/v2/location/find/suggestions?query=<name>&limit=<n>`, answers
  JSON `[{id, name, sefName, type, parent: {name}}]`. Homonyms carry a district suffix in `sefName`
  ("Závadka" -> `zavadka-humenne`, `zavadka-gelnica`), which is why the URL can't be guessed.
- The listing, `/vysledky/pozemky/<sefName>/predaj?priceTo=&areaFrom=&order=NEWEST|PRICE_ASC`, is
  server-rendered (Next.js). An unknown `sefName` silently shows the whole country ("Pozemky na
  predaj" without a place in the `h1`), which is how a mismatch is caught.
- Offers are read from the page's schema.org JSON-LD (`SearchResultsPage` -> `ItemList`: name,
  category, url, floorSize, validFrom), which is stable, unlike the generated MUI class names. The
  price comes from the tile's own text instead: for "price per m² only" offers the JSON-LD `price`
  holds the per-m² amount. The tile shows the seller's name and a description; neither is read.
- An offer page is server-rendered too, with JSON-LD (`Offer`, `Accommodation`, `ItemPage` dates);
  its parameter table is label/value paragraphs ("Plocha pozemku:" / "1 935 m²"). A removed offer
  redirects to `/vysledky`.
"""

import json
from urllib.parse import quote, urlencode

from pydantic import BaseModel, ValidationError

from web_agent.models import ListingSearchRequest, ListingSort
from web_agent.portals.base import MarkerPortal
from web_agent.portals.common import LocationMatch, LocationMiss, pick_location, slugify

BASE_URL = "https://www.nehnutelnosti.sk"
SORT_PARAMS: dict[ListingSort, str] = {"newest": "NEWEST", "price_asc": "PRICE_ASC"}
LOOKUP_LIMIT = 10

EXCLUDED_CONTAINERS = (
    '[class*="comment"]',
    '[id*="comment"]',
    '[class*="similar"]',
    '[data-test-id*="similar"]',
    "form",
)

# Shared by both scripts: the JSON-LD objects of the page, flattened out of their @graph.
_JSON_LD = """
    const jsonLd = [];
    for (const script of document.querySelectorAll('script[type="application/ld+json"]')) {
        try {
            const data = JSON.parse(script.textContent);
            for (const item of [].concat(data)) jsonLd.push(...(item["@graph"] || [item]));
        } catch (e) {}
    }
    const typed = (type) => jsonLd.filter((item) => [].concat(item["@type"]).includes(type));
"""

EXTRACT_JS = f"""
(() => {{
    const EXCLUDED = {json.dumps(", ".join(EXCLUDED_CONTAINERS))};
    const excluded = (el) => el.closest(EXCLUDED) !== null;
    {_JSON_LD}
    const page = typed("SearchResultsPage")[0];
    const items = page && page.mainEntity ? page.mainEntity.itemListElement || [] : [];
    // The tile of one offer: the widest element holding links to that offer only.
    const tileOf = (url) => {{
        const link = Array.from(document.querySelectorAll("a")).find((a) => a.href === url);
        if (!link) return null;
        const offers = (el) => new Set(Array.from(el.querySelectorAll('a[href*="/detail/"]')).map((a) => a.href)).size;
        let tile = link;
        while (tile.parentElement && offers(tile.parentElement) <= 1) tile = tile.parentElement;
        return excluded(tile) ? null : tile;
    }};
    const leaves = (tile) => Array.from(tile.querySelectorAll("p"))
        .filter((p) => p.children.length === 0 && !excluded(p)).map((p) => p.innerText.trim());
    const entries = items.map((element) => {{
        const item = element.item || {{}};
        const tile = item.url ? tileOf(item.url) : null;
        const texts = tile ? leaves(tile) : [];
        const heading = tile ? tile.querySelector("h2") : null;
        const after = heading ? Array.from(tile.querySelectorAll("p")).find(
            (p) => p.children.length === 0 && (heading.compareDocumentPosition(p) & Node.DOCUMENT_POSITION_FOLLOWING)
        ) : null;
        const offeredBy = item.offeredBy && item.offeredBy["@id"] || "";
        return {{
            title: item.name || "",
            href: item.url || "",
            location: after ? after.innerText : null,
            price: texts.find((t) => /€\\s*$/.test(t) && !/m²|m2/.test(t)) || null,
            price_per_m2: texts.find((t) => /€\\s*\\/\\s*m/.test(t)) || null,
            area: item.floorSize ? String(item.floorSize.value) : null,
            plot_type: item.category || null,
            listed: item.validFrom || null,
            seller: offeredBy.includes("/RealEstateAgent/") ? "agency" : null,
        }};
    }});
    const heading = document.querySelector("h1");
    const count = Array.from(document.querySelectorAll("p, span, h2, div"))
        .filter((el) => el.children.length === 0)
        .map((el) => el.innerText.trim())
        .find((t) => /^\\d[\\d\\s\\u00a0]*\\s+(nehnuteľnost|inzerát)/i.test(t));
    return {{
        entries,
        heading: heading ? heading.innerText : null,
        count: count || (page && page.mainEntity && items.length === 0 ? "0" : null),
        no_results: Boolean(page) && items.length === 0,
    }};
}})()
"""

DETAILS_JS = f"""
(() => {{
    const EXCLUDED = {json.dumps(", ".join(EXCLUDED_CONTAINERS))};
    const excluded = (el) => el.closest(EXCLUDED) !== null;
    {_JSON_LD}
    const title = document.querySelector("h1");
    if (!title || !location.pathname.startsWith("/detail/")) return {{offer: null, params: []}};
    const following = (el) => Array.from(document.querySelectorAll("p")).filter(
        (p) => p.children.length === 0 && !excluded(p) && (el.compareDocumentPosition(p) & Node.DOCUMENT_POSITION_FOLLOWING)
    ).map((p) => p.innerText.trim());
    const texts = following(title);
    const params = [];
    for (const label of document.querySelectorAll("p")) {{
        const value = label.nextElementSibling;
        const name = label.innerText.trim();
        if (!name.endsWith(":") || !value || value.tagName !== "P" || excluded(label)) continue;
        params.push({{name, value: value.innerText}});
    }}
    const offer = typed("Offer")[0] || {{}};
    const place = typed("Accommodation")[0] || {{}};
    const itemPage = typed("ItemPage")[0] || {{}};
    const kind = Array.from(document.querySelectorAll("h2")).map((h) => h.innerText.trim())
        .find((t) => / na predaj$/.test(t));
    const offeredBy = offer.offeredBy && offer.offeredBy["@id"] || "";
    return {{
        offer: {{
            title: title.innerText,
            href: location.href,
            location: texts[0] || null,
            price: texts.find((t) => /€\\s*$/.test(t) && !/m²|m2/.test(t)) || null,
            price_per_m2: texts.find((t) => /€\\s*\\/\\s*m/.test(t)) || null,
            area: place.floorSize ? String(place.floorSize.value) : null,
            plot_type: kind ? kind.replace(/ na predaj$/, "") : null,
            listed: itemPage.dateCreated || null,
            updated: itemPage.datePublished || null,
            seller: offeredBy.includes("/RealEstateAgent/") ? "agency" : null,
        }},
        params,
    }};
}})()
"""

INACTIVE_MARKERS = ("Inzerát už nie je aktívny", "Tento inzerát už neexistuje")  # PROVISIONAL


class _Parent(BaseModel):
    name: str


class _Suggestion(BaseModel):
    name: str
    sefName: str
    type: str
    parent: _Parent | None = None


class NehnutelnostiSkPortal(MarkerPortal):
    name = "nehnutelnosti.sk"
    base_url = BASE_URL
    hosts = frozenset({"www.nehnutelnosti.sk"})
    extra_origins = frozenset()
    currency = "EUR"
    extract_js = EXTRACT_JS
    details_js = DETAILS_JS
    inactive_markers = INACTIVE_MARKERS
    status_block_reason = "nehnutelnosti.sk refused the request (HTTP {status})"

    def lookup_url(self, location: str) -> str:
        return f"{BASE_URL}/api/v2/location/find/suggestions?{urlencode({'query': location, 'limit': LOOKUP_LIMIT})}"

    def resolve_location(self, lookup: object, location: str) -> LocationMatch | LocationMiss | None:
        if not isinstance(lookup, list):
            return None
        cities: list[_Suggestion] = []
        for item in lookup:
            try:
                suggestion = _Suggestion.model_validate(item)
            except ValidationError:
                continue
            # A municipality; its `sefName` goes into the URL path, so it must already be a slug.
            if suggestion.type == "CITY" and slugify(suggestion.sefName) == suggestion.sefName:
                cities.append(suggestion)
        match = pick_location(cities, location, name=lambda city: city.name, label=_label)
        if isinstance(match, LocationMiss):
            return match
        return LocationMatch(key=match.sefName, name=_label(match), place=match.name)

    def url_for(self, request: ListingSearchRequest, location: LocationMatch) -> str:
        params: dict[str, str | int] = {}
        if request.max_price:
            params["priceTo"] = request.max_price
        if request.min_area_m2:
            params["areaFrom"] = request.min_area_m2
        if sort := SORT_PARAMS.get(request.sort):
            params["order"] = sort
        url = f"{BASE_URL}/vysledky/pozemky/{quote(location.key)}/predaj"
        return f"{url}?{urlencode(params)}" if params else url

    def is_no_results(self, text: str) -> bool:
        return False


def _label(suggestion: _Suggestion) -> str:
    return f"{suggestion.name} ({suggestion.parent.name})" if suggestion.parent else suggestion.name
