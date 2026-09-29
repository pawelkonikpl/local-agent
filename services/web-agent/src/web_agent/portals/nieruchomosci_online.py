"""nieruchomosci-online.pl: building plots for sale in one locality.

Checked on 28.09.2026 in a headless Chromium (listing, search, offer, archived and removed offer):
- The portal's autocomplete, `/?module=locationTip&type=cityQuarterTip&cn=<name>`, answers JSON
  (only to requests with `X-Requested-With: XMLHttpRequest`, i.e. a fetch, not a navigation)
  `[{id, cn, rn, idQ}]` (`rn`: gmina or powiat; `idQ` > 0 for a district of a town), or `false`.
  Homonyms come as separate entries ("Czarna Góra", gm. Bukowina Tatrzańska / pow. elbląski / ...),
  the most popular first.
- The search page, `/szukaj.html?3,dzialka,sprzedaz,,<name>:<id>,<quarter>,<street>,<distance>,
  <price from>-<price to>,<area from>-<area to>&o=<sort>`, is complete at the load event. The city
  id wins over the name: a wrong id shows another town, named in the `h1` ("Działki na sprzedaż
  Groń małopolskie"), which is how a mismatch is caught.
- Tiles (`div.tile[data-id]`) mix active offers with archived ones (`data-pie="archive"`, no link)
  and offers from other towns (`data-pie="searchSupplement"`); only the rest are read. The first
  two tiles of any list are `data-pie="prime"` whatever they are, and no tile carries a visible
  "promoted" label, so no offer is flagged `sponsored`.
- "Nothing found" is a visible `.box-offer-not-found`; the box is in the page even with results.
- An offer page is server-rendered; an archived offer says "Ogłoszenie archiwalne", a removed one
  "chyba ktoś głodny zjadł tę stronę" (HTTP 200 for both).
- Read from the DOM: the page has no offer JSON beyond analytics.
"""

import json
from urllib.parse import quote

from pydantic import BaseModel, ValidationError

from web_agent.models import ListingSearchRequest, ListingSort
from web_agent.portals.base import MarkerPortal
from web_agent.portals.common import LocationMatch, LocationMiss, pick_location

BASE_URL = "https://www.nieruchomosci-online.pl"
SORT_PARAMS: dict[ListingSort, str] = {"newest": "modDate,desc", "price_asc": "price,asc"}

# Selectors are the most fragile part of this portal; keep them all here.
TILE_SELECTOR = "div.tile[data-id]"
SKIPPED_TILES = ("archive", "searchSupplement")
EXCLUDED_CONTAINERS = (
    '[id*="komentarz"]',
    '[class*="comment"]',
    '[class*="similar"]',
    '[id*="similar"]',
    '[class*="contact"]',
    '[id*="contact"]',
)

EXTRACT_JS = f"""
(() => {{
    const EXCLUDED = {json.dumps(", ".join(EXCLUDED_CONTAINERS))};
    const SKIPPED = {json.dumps(SKIPPED_TILES)};
    const excluded = (el) => el.closest(EXCLUDED) !== null;
    const text = (root, selector) => {{
        const el = root.querySelector(selector);
        return el && !excluded(el) ? el.innerText : null;
    }};
    const attribute = (tile, label) => {{
        for (const row of tile.querySelectorAll(".attributes__box--item p")) {{
            const name = row.querySelector("span"), value = row.querySelector("strong");
            if (name && value && name.innerText.trim().replace(/:$/, "") === label) return value.innerText;
        }}
        return null;
    }};
    const entries = Array.from(document.querySelectorAll({json.dumps(TILE_SELECTOR)}))
        .filter((tile) => !SKIPPED.includes(tile.dataset.pie || "") && !excluded(tile))
        .map((tile) => {{
            const link = tile.querySelector("h2 a");
            if (!link || excluded(link)) return null;
            return {{
                title: link.innerText,
                href: link.getAttribute("href") || "",
                location: text(tile, ".province"),
                price: text(tile, ".primary-display > span:first-child"),
                area: text(tile, ".primary-display .area"),
                price_per_m2: text(tile, '.primary-display [id^="secondary-display"]'),
                plot_type: attribute(tile, "Rodzaj działki"),
            }};
        }})
        .filter((entry) => entry !== null);
    const notFound = Array.from(document.querySelectorAll(".box-offer-not-found"))
        .some((box) => box.offsetParent !== null);
    return {{
        entries,
        heading: text(document, "h1"),
        count: text(document, "#boxOfCounter"),
        no_results: notFound && entries.length === 0,
    }};
}})()
"""

# Only these rows of the offer's tables; the seller's name, phone and the description are never read.
DETAILS_JS = f"""
(() => {{
    const EXCLUDED = {json.dumps(", ".join(EXCLUDED_CONTAINERS))};
    const excluded = (el) => el.closest(EXCLUDED) !== null;
    const text = (selector) => {{
        const el = document.querySelector(selector);
        return el && !excluded(el) ? el.innerText : null;
    }};
    const title = text("#boxOffTop h1") || text("h1.h1Title");
    if (!title) return {{offer: null, params: []}};
    const params = [];
    for (const item of document.querySelectorAll("#attributesTable .box__attributes--item")) {{
        const name = item.querySelector(".fheader"), value = item.querySelector(".fsize-c");
        if (name && value && !excluded(item)) params.push({{name: name.innerText, value: value.innerText}});
    }}
    // "Źródło: biuro nieruchomości: <name>, zaktualizowane: 18.09.2026": only the kind and the date.
    let seller = null, updated = null, previous = null;
    for (const row of document.querySelectorAll("#detailsTable li, #locationTable li")) {{
        if (excluded(row)) continue;
        const name = (row.querySelector("strong") || {{innerText: ""}}).innerText.trim();
        const value = (row.querySelector("span") || {{innerText: ""}}).innerText.trim();
        if (name.startsWith("Źródło")) {{
            seller = value.split(":")[0];
            updated = (value.match(/\\d{{2}}\\.\\d{{2}}\\.\\d{{4}}/) || [null])[0];
            previous = null;
        }} else if (row.classList.contains("empty")) {{
            if (previous) previous.value += "; " + value;
        }} else {{
            previous = {{name, value}};
            params.push(previous);
        }}
    }}
    return {{
        offer: {{
            title,
            href: location.href,
            location: text("#boxOffTop h2"),
            price: text(".info-price .info-primary-price") || text(".info-primary-price"),
            area: text(".info-price .info-area") || text(".info-area"),
            price_per_m2: text(".info-price .info-secondary-price") || text(".info-secondary-price"),
            plot_type: (params.find((p) => p.name.replace(/:$/, "") === "Rodzaj działki") || {{}}).value || null,
            updated,
            seller,
        }},
        params,
    }};
}})()
"""

INACTIVE_MARKERS = ("Ogłoszenie archiwalne", "chyba ktoś głodny zjadł tę stronę")


class _Tip(BaseModel):
    id: str
    cn: str
    rn: str | None = None
    idQ: int = 0


class NieruchomosciOnlinePortal(MarkerPortal):
    name = "nieruchomosci-online.pl"
    base_url = BASE_URL
    hosts = frozenset({"www.nieruchomosci-online.pl"})
    # jQuery from cdnjs and the portal's own scripts render the search page.
    extra_origins = frozenset({"https://s.st-nieruchomosci-online.pl:443", "https://cdnjs.cloudflare.com:443"})
    currency = "PLN"
    extract_js = EXTRACT_JS
    details_js = DETAILS_JS
    no_results_markers = ()
    inactive_markers = INACTIVE_MARKERS
    status_block_reason = "nieruchomosci-online.pl refused the request (HTTP {status})"

    def lookup_url(self, location: str) -> str:
        return f"{BASE_URL}/?module=locationTip&type=cityQuarterTip&cn={quote(location.lower(), safe='')}"

    def resolve_location(self, lookup: object, location: str) -> LocationMatch | LocationMiss | None:
        if lookup is False:
            return LocationMiss()
        if not isinstance(lookup, list):
            return None
        tips: list[_Tip] = []
        for item in lookup:
            try:
                tip = _Tip.model_validate(item)
            except ValidationError:
                continue
            # A town or village, not a district of one; the id goes into the URL.
            if tip.idQ == 0 and tip.id.isdigit():
                tips.append(tip)
        match = pick_location(tips, location, name=lambda tip: tip.cn, label=_label)
        if isinstance(match, LocationMiss):
            return match
        return LocationMatch(key=f"{quote(match.cn, safe='')}:{match.id}", name=_label(match), place=match.cn)

    def url_for(self, request: ListingSearchRequest, location: LocationMatch) -> str:
        price = f"-{request.max_price}" if request.max_price else ""
        area = str(request.min_area_m2) if request.min_area_m2 else ""
        criteria = ",".join(["3", "dzialka", "sprzedaz", "", location.key, "", "", "", price, area]).rstrip(",")
        url = f"{BASE_URL}/szukaj.html?{criteria}"
        if sort := SORT_PARAMS.get(request.sort):
            url += f"&o={sort}"
        return url

    def is_no_results(self, text: str) -> bool:
        # The marker is in the page even with results; `extract_js` reports the visible state.
        return False


def _label(tip: _Tip) -> str:
    return f"{tip.cn} ({tip.rn})" if tip.rn else tip.cn
