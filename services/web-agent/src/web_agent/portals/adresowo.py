"""adresowo.pl: plots for sale in one locality ("bez pośredników" and agency offers).

Checked on 28.09.2026 with a plain HTTP client and a headless Chromium (listing, offer, 404):
- The portal's autocomplete, `/offer-list/ajax/location-hints/?path=&q=<name>`, answers
  `{items: [{location: "małopolskie, Korbielów", accuracyLevel, counts: {dzialki: {url}}}]}`; the
  listing path carries a suffix that can't be guessed (`/dzialki/korbielow-4/`). An unknown path
  redirects to `/f/dzialki/<slug>/`, a nationwide list whose `h1` names no place.
- Filters and sort are POSTed forms, not URL parameters: the price and area limits are applied
  to the parsed offers, and `price_asc` sorts the first page (the portal's own order otherwise).
- The locality's own offers are `#offer-list-results [data-offer-card]`; the cards below it
  ("Oferty z najbliższej okolicy") are other places and are never read. Cards have no free-text
  title: the photo's `alt`, which the portal generates ("Działka rekreacyjna Białka Tatrzańska,
  ul. Środkowa"), is the title, and the word between "Działka" and the place is the plot type.
- An offer page has no parameter table: the header's value/label pairs ("rekreacyjna" /
  "typ działki") and the JSON-LD `Offer` (price) and `Place` (address, "... Działka rekreacyjna -
  1 200 m²") are all there is. A removed offer is HTTP 404.
"""

import json
import re
from urllib.parse import urlencode

from pydantic import BaseModel, ValidationError

from web_agent.models import ListingSearchRequest
from web_agent.portals.base import MarkerPortal
from web_agent.portals.common import LocationMatch, LocationMiss, pick_location

BASE_URL = "https://adresowo.pl"
LISTING_PATH = re.compile(r"/dzialki/[A-Za-z0-9_-]+/")

EXCLUDED_CONTAINERS = ('[aria-label="Opis nieruchomości"]', "#descriptionContainer", "form", '[id^="modal"]')

EXTRACT_JS = f"""
(() => {{
    const EXCLUDED = {json.dumps(", ".join(EXCLUDED_CONTAINERS))};
    const excluded = (el) => el.closest(EXCLUDED) !== null;
    const cards = Array.from(document.querySelectorAll("#offer-list-results [data-offer-card]"));
    const entries = cards.filter((card) => !excluded(card)).map((card) => {{
        const link = card.querySelector("h2 a");
        if (!link) return null;
        // The place and street lines, then "Działka na sprzedaż bez pośredników" / "przez agenta".
        const [place, street] = Array.from(link.querySelectorAll(":scope > span.line-clamp-1"))
            .map((s) => s.innerText.trim());
        const subtitle = link.querySelector(":scope > span:last-child");
        const alt = ((card.querySelector("img") || {{}}).alt || "").trim();
        const rest = alt.replace(/^Działka\\s+/, "");
        const values = Array.from(card.querySelectorAll("p.flex-auto")).map((p) => p.innerText.trim());
        return {{
            title: alt || [place, street].filter(Boolean).join(", "),
            href: link.getAttribute("href") || "",
            location: [place, street].filter(Boolean).join(", ") || null,
            price: values.find((v) => /zł|€/.test(v)) || null,
            area: values.find((v) => /m²|ha$/.test(v)) || null,
            plot_type: rest && place && !rest.startsWith(place) ? rest.split(/\\s+/)[0] : null,
            seller: subtitle ? subtitle.innerText : null,
        }};
    }}).filter((entry) => entry !== null);
    const count = Array.from(document.querySelectorAll("span, p"))
        .filter((el) => el.children.length === 0)
        .map((el) => el.textContent.trim())
        .find((t) => /^\\d[\\d\\s]*\\s+ofert/.test(t));
    const heading = document.querySelector("h1");
    return {{
        entries,
        heading: heading ? heading.textContent.replace(/\\s+/g, " ").trim() : null,
        count: count || null,
        no_results: false,
    }};
}})()
"""

DETAILS_JS = f"""
(() => {{
    const EXCLUDED = {json.dumps(", ".join(EXCLUDED_CONTAINERS))};
    const excluded = (el) => el.closest(EXCLUDED) !== null;
    const heading = document.querySelector("h1");
    if (!heading || !location.pathname.startsWith("/o/")) return {{offer: null, params: []}};
    const jsonLd = [];
    for (const script of document.querySelectorAll('script[type="application/ld+json"]')) {{
        try {{
            const data = JSON.parse(script.textContent);
            for (const item of [].concat(data)) jsonLd.push(...(item["@graph"] || [item]));
        }} catch (e) {{}}
    }}
    const offer = jsonLd.find((item) => item["@type"] === "Offer") || {{}};
    const place = jsonLd.find((item) => item["@type"] === "Place") || {{}};
    // The header's value/label pairs, e.g. "rekreacyjna" / "typ działki".
    const params = [];
    for (const column of document.querySelectorAll("div.flex-col")) {{
        const spans = Array.from(column.children).filter((el) => el.tagName === "SPAN" && el.children.length === 0);
        if (column.children.length !== 2 || spans.length !== 2 || excluded(column)) continue;
        params.push({{name: spans[1].textContent.trim(), value: spans[0].textContent.trim()}});
    }}
    const title = heading.textContent.replace(/\\s+/g, " ").trim();
    const kind = (params.find((p) => p.name === "typ działki") || {{}}).value || null;
    return {{
        offer: {{
            title,
            href: location.href,
            location: (place.address || {{}}).streetAddress || null,
            price: offer.price != null ? `${{offer.price}} ${{offer.priceCurrency || ""}}` : null,
            area: place.name || null,
            plot_type: kind,
            seller: title,
        }},
        params,
    }};
}})()
"""

# PROVISIONAL: to be confirmed on a saved page of an ended offer.
INACTIVE_MARKERS = ("Ogłoszenie nieaktualne", "Ogłoszenie zostało zakończone")


class _Link(BaseModel):
    url: str


class _Hint(BaseModel):
    location: str
    locationURL: str
    accuracyLevel: str
    counts: dict[str, _Link] = {}

    @property
    def place(self) -> str:
        """"małopolskie, Białka Tatrzańska" -> "Białka Tatrzańska"."""
        return self.location.split(", ", 1)[-1]

    @property
    def listing_path(self) -> str:
        if "dzialki" in self.counts:
            return self.counts["dzialki"].url
        return self.locationURL.replace("/nieruchomosci/", "/dzialki/", 1)


class _Hints(BaseModel):
    items: list[dict] = []


class AdresowoPortal(MarkerPortal):
    name = "adresowo.pl"
    base_url = BASE_URL
    hosts = frozenset({"adresowo.pl"})
    extra_origins = frozenset()
    currency = "PLN"
    extract_js = EXTRACT_JS
    details_js = DETAILS_JS
    inactive_markers = INACTIVE_MARKERS
    local_sorts = frozenset({"price_asc"})
    status_block_reason = "adresowo.pl refused the request (HTTP {status})"

    def lookup_url(self, location: str) -> str:
        return f"{BASE_URL}/offer-list/ajax/location-hints/?{urlencode({'path': '', 'q': location})}"

    def resolve_location(self, lookup: object, location: str) -> LocationMatch | LocationMiss | None:
        try:
            hints = _Hints.model_validate(lookup)
        except ValidationError:
            return None
        towns: list[_Hint] = []
        for item in hints.items:
            try:
                hint = _Hint.model_validate(item)
            except ValidationError:
                continue
            # A town or village (not a street or district) whose listing path is a plain one.
            if hint.accuracyLevel == "city" and LISTING_PATH.fullmatch(hint.listing_path):
                towns.append(hint)
        match = pick_location(towns, location, name=lambda hint: hint.place, label=lambda hint: hint.location)
        if isinstance(match, LocationMiss):
            return match
        return LocationMatch(key=match.listing_path, name=match.location, place=match.place)

    def url_for(self, request: ListingSearchRequest, location: LocationMatch) -> str:
        return f"{BASE_URL}{location.key}"

    def is_no_results(self, text: str) -> bool:
        return False
