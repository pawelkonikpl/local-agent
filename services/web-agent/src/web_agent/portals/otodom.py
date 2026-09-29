"""otodom.pl: plots for sale ("Działki na sprzedaż") in one locality.

Checked on 28.09.2026 in a headless Chromium (listing with filters, offer, ended offer):
- Listing URLs are a path of location ids, `/pl/wyniki/sprzedaz/dzialka/<voivodeship>/<county>/
  <gmina>/<village>` (anything shorter is a 404), so a locality must be looked up. The portal's
  GraphQL endpoint answers the `autocomplete` query over GET, `/api/query?query=...&variables=...`:
  `{data: {autocomplete: {locationsObjects: [{id, name, detailedLevel, fullName}]}}}`, the `id`
  being that path. PROVISIONAL: a query of our own, not the portal's persisted one -- if the portal
  stops accepting it, the lookup reports an unexpected answer instead of guessing.
- Filters and sort: `?priceMax=&areaMin=&by=LATEST&direction=DESC` (`by=PRICE&direction=ASC`).
- Offers are read from the page's `__NEXT_DATA__` (`props.pageProps.data.searchAds`), allowlisted
  fields only (title, slug, price, area, price per m², location, dates, private owner, promoted).
  The list also carries offers from outside the locality and duplicates of promoted ones: only
  offers whose reverse geocoding includes the searched location are kept, deduplicated by URL.
- An offer page: `__NEXT_DATA__` `ad` (title, location, dates, advertiser type, status) and the
  schema.org JSON-LD `Product` (`offers.price`, `additionalProperty`: the parameter table in
  Polish, e.g. "Typ działki" -> "budowlana"). An ended offer is HTTP 410 with `ad.status`
  "outdated". Its description (in both JSONs) is never read.
"""

import json
from urllib.parse import quote, urlencode

from pydantic import BaseModel, ValidationError

from web_agent.models import ListingSearchRequest, ListingSort
from web_agent.portals.base import MarkerPortal
from web_agent.portals.common import LocationMatch, LocationMiss, pick_location

BASE_URL = "https://www.otodom.pl"
SORT_PARAMS: dict[ListingSort, tuple[str, str]] = {"newest": ("LATEST", "DESC"), "price_asc": ("PRICE", "ASC")}
AUTOCOMPLETE_QUERY = (
    "query($q: String!) { autocomplete(query: $q) "
    "{ ... on FoundLocations { locationsObjects { id name detailedLevel fullName } } } }"
)
# Levels of a town or village (not a street, district or region).
PLACE_LEVELS = frozenset({"city", "village", "city_or_village", "town"})

EXTRACT_JS = """
(() => {
    const node = document.getElementById("__NEXT_DATA__");
    if (!node) return {entries: [], heading: null, count: null, no_results: false};
    const props = (JSON.parse(node.textContent).props || {}).pageProps || {};
    const ads = (props.data || {}).searchAds || {items: [], pagination: {}};
    const wanted = typeof props.location === "string" ? props.location : null;
    const money = (m) => m && m.value != null ? `${m.value} ${m.currency || ""}` : null;
    const entries = (ads.items || [])
        .filter((ad) => !wanted || ((ad.location || {}).reverseGeocoding || {locations: []})
            .locations.some((place) => place.id === wanted))
        .map((ad) => {
            const address = (ad.location || {}).address || {};
            const place = [address.city && address.city.name, address.province && address.province.name]
                .filter(Boolean).join(", ");
            return {
                title: ad.title || "",
                href: ad.slug ? `/pl/oferta/${ad.slug}` : "",
                location: place || null,
                price: ad.hidePrice ? null : money(ad.totalPrice),
                area: String(ad.areaInSquareMeters || ad.terrainAreaInSquareMeters || "") || null,
                price_per_m2: money(ad.pricePerSquareMeter),
                listed: ad.createdAtFirst || null,
                updated: ad.pushedUpAt || ad.dateCreated || null,
                seller: ad.isPrivateOwner ? "private" : (ad.agency ? "agency" : null),
                sponsored: Boolean(ad.isPromoted),
            };
        });
    const total = (ads.pagination || {}).totalItems;
    return {
        entries,
        heading: props.locationName || null,
        count: total != null ? String(total) : null,
        no_results: total === 0,
    };
})()
"""

DETAILS_JS = """
(() => {
    const node = document.getElementById("__NEXT_DATA__");
    const ad = node ? ((JSON.parse(node.textContent).props || {}).pageProps || {}).ad : null;
    if (!ad) return {offer: null, params: []};
    if (ad.status !== "active" || ad.shouldShowExpiredAdPage) return {offer: null, params: [], inactive: true};
    let product = null;
    for (const script of document.querySelectorAll('script[type="application/ld+json"]')) {
        try {
            const data = JSON.parse(script.textContent);
            for (const item of [].concat(data).flatMap((d) => d["@graph"] || [d])) {
                if (item.additionalProperty) product = item;
            }
        } catch (e) {}
    }
    const params = product ? product.additionalProperty.map((p) => ({name: p.name, value: String(p.value)})) : [];
    const value = (name) => (params.find((p) => p.name === name) || {}).value || null;
    const offers = product && product.offers || {};
    const perM2 = offers.priceSpecification;
    const places = ((ad.location || {}).reverseGeocoding || {}).locations || [];
    return {
        offer: {
            title: ad.title || "",
            href: location.href,
            location: places.length ? places[places.length - 1].fullName : null,
            price: offers.price != null ? `${offers.price} ${offers.priceCurrency || ""}` : null,
            area: value("Powierzchnia"),
            price_per_m2: perM2 && perM2.price != null ? `${perM2.price} ${perM2.priceCurrency || ""}` : null,
            plot_type: value("Typ działki"),
            listed: ad.createdAt || null,
            updated: ad.modifiedAt || null,
            seller: ad.advertiserType || null,
        },
        params,
    };
})()
"""


class _Place(BaseModel):
    id: str
    name: str
    detailedLevel: str
    fullName: str


class _Autocomplete(BaseModel):
    locationsObjects: list[dict]


class _Data(BaseModel):
    autocomplete: _Autocomplete


class _Answer(BaseModel):
    data: _Data


class OtodomPortal(MarkerPortal):
    name = "otodom.pl"
    base_url = BASE_URL
    hosts = frozenset({"www.otodom.pl"})
    extra_origins = frozenset()
    currency = "PLN"
    extract_js = EXTRACT_JS
    details_js = DETAILS_JS
    status_block_reason = "otodom.pl bot protection (HTTP {status})"
    block_markers = ("The request could not be satisfied",)
    marker_block_reason = "otodom.pl bot protection"

    def lookup_url(self, location: str) -> str:
        variables = json.dumps({"q": location}, ensure_ascii=False)
        return f"{BASE_URL}/api/query?{urlencode({'query': AUTOCOMPLETE_QUERY, 'variables': variables})}"

    def resolve_location(self, lookup: object, location: str) -> LocationMatch | LocationMiss | None:
        try:
            answer = _Answer.model_validate(lookup)
        except ValidationError:
            return None
        places: list[_Place] = []
        for item in answer.data.autocomplete.locationsObjects:
            try:
                place = _Place.model_validate(item)
            except ValidationError:
                continue
            # The id is the URL path: lowercase letters, digits, "-" and "/" only.
            if place.detailedLevel in PLACE_LEVELS and all(
                segment and segment.replace("-", "").isalnum() and segment.isascii() and segment.islower()
                or segment.replace("-", "").isdigit()
                for segment in place.id.split("/")
            ):
                places.append(place)
        match = pick_location(places, location, name=lambda place: place.name, label=lambda place: place.fullName)
        if isinstance(match, LocationMiss):
            return match
        return LocationMatch(key=match.id, name=match.fullName, place=match.name)

    def url_for(self, request: ListingSearchRequest, location: LocationMatch) -> str:
        params: dict[str, str | int] = {}
        if request.max_price:
            params["priceMax"] = request.max_price
        if request.min_area_m2:
            params["areaMin"] = request.min_area_m2
        if sort := SORT_PARAMS.get(request.sort):
            params["by"], params["direction"] = sort
        url = f"{BASE_URL}/pl/wyniki/sprzedaz/dzialka/{quote(location.key)}"
        return f"{url}?{urlencode(params)}" if params else url

    def is_no_results(self, text: str) -> bool:
        return False
