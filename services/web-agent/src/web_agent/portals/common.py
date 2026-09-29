"""What every portal shares: parsing the texts a portal shows (prices, areas, dates), matching a
locality by name, and turning raw offers into clean, screened `Listing`s.

Pure functions, no browser: every portal is testable on saved pages and plain strings. Today's
date comes from an injected clock, so "dzisiaj" and "wczoraj" are testable too.
"""

import json
import logging
import re
import unicodedata
from collections.abc import Callable, Iterable, Iterator
from datetime import UTC, date, datetime, timedelta
from decimal import ROUND_HALF_UP, Decimal, InvalidOperation

from contracts.listings import MAX_PARAMS, PARAM_VALUE_MAX_CHARS
from contracts.web_agent import SPONSORED_FLAG
from pydantic import BaseModel, ValidationError

from web_agent.engines.common import screen, without_query
from web_agent.models import Currency, Listing, ListingParam, ListingSearchRequest

logger = logging.getLogger(__name__)

Clock = Callable[[], date]

TITLE_MAX_CHARS = 200
LISTING_LOCATION_MAX_CHARS = 150
PLOT_TYPE_MAX_CHARS = 60
CENT = Decimal("0.01")


def today_utc() -> date:
    return datetime.now(UTC).date()


# --- locality names ---------------------------------------------------------------------------

# Letters NFKD doesn't split into a base letter and a diacritic.
_TRANSLITERATION = str.maketrans({"ł": "l", "Ł": "l", "đ": "d", "Đ": "d", "ß": "ss", "ø": "o", "Ø": "o"})
_NON_SLUG = re.compile(r"[^a-z0-9]+")


def slugify(text: str) -> str:
    """`Białka Tatrzańska` -> `bialka-tatrzanska`: lowercase ASCII letters, digits and single
    hyphens only, whatever the input, so a slug can't carry anything else into a URL."""
    ascii_text = (
        unicodedata.normalize("NFKD", text.translate(_TRANSLITERATION)).encode("ascii", "ignore").decode("ascii")
    )
    return _NON_SLUG.sub("-", ascii_text.lower()).strip("-")


def same_place(a: str, b: str) -> bool:
    """Whether two locality names are the same after transliteration ("Groń" == "gron")."""
    return bool(slugify(a)) and slugify(a) == slugify(b)


def names_place(text: str, place: str) -> bool:
    """Whether `text` (a page heading, breadcrumbs) names `place`: its slug tokens appear in order,
    as whole tokens, so "Groń" doesn't match "Gronowo"."""
    tokens, wanted = slugify(text).split("-"), slugify(place).split("-")
    if not wanted or wanted == [""]:
        return False
    return any(tokens[i : i + len(wanted)] == wanted for i in range(len(tokens) - len(wanted) + 1))


class LocationMatch(BaseModel):
    """A locality the portal knows: `key` is what its listing URL needs (a slug, an id, a path),
    `name` what to show the model, e.g. "Groń (gm. Bukowina Tatrzańska)"."""

    key: str
    name: str
    # The bare locality name, to check the results page against.
    place: str


class LocationMiss(BaseModel):
    """The portal has no such locality; up to 3 names it suggested instead."""

    suggestions: list[str] = []


SUGGESTIONS_KEPT = 3


def pick_location[T](
    candidates: Iterable[T], location: str, *, name: Callable[[T], str], label: Callable[[T], str]
) -> T | LocationMiss:
    """The first candidate whose name is `location` after transliteration; else the portal's first
    few suggestions, by `label`, for the model to rephrase the query."""
    candidates = list(candidates)
    for candidate in candidates:
        if same_place(name(candidate), location):
            return candidate
    suggestions: list[str] = []
    for candidate in candidates:
        text = screen(label=label(candidate))
        if not text.withheld and text.texts["label"] and text.texts["label"] not in suggestions:
            suggestions.append(text.texts["label"][:LISTING_LOCATION_MAX_CHARS])
        if len(suggestions) >= SUGGESTIONS_KEPT:
            break
    return LocationMiss(suggestions=suggestions)


def parse_json(text: str | None) -> object | None:
    """A JSON document the browser displays as text (a lookup endpoint), or None if it isn't JSON."""
    if not text:
        return None
    try:
        return json.loads(text)
    except ValueError:
        return None


# --- numbers, money, areas --------------------------------------------------------------------

_SPACES = re.compile(r"[\s  ]")
_NUMBER = re.compile(r"\d[\d\s  .,]*")


def parse_decimal(text: str) -> Decimal | None:
    """The first number in `text`, in Polish or English notation: "290 000", "333,33", "4259.17",
    "1.214" (thousands), "0,87"."""
    match = _NUMBER.search(text)
    if match is None:
        return None
    number = _SPACES.sub("", match.group()).rstrip(".,")
    if re.fullmatch(r"\d+", number):
        return Decimal(number)
    if decimal := re.fullmatch(r"(\d+)[.,](\d{1,2})", number):
        return Decimal(f"{decimal[1]}.{decimal[2]}")
    grouped = re.fullmatch(r"(\d{1,3}(?:([.,])\d{3})+)(?:[.,](\d{1,2}))?", number)
    if grouped:
        whole = grouped[1].replace(grouped[2], "")
        return Decimal(f"{whole}.{grouped[3]}" if grouped[3] else whole)
    return None


_CURRENCIES: tuple[tuple[re.Pattern[str], Currency], ...] = (
    (re.compile(r"zł|pln", re.IGNORECASE), "PLN"),
    (re.compile(r"€|eur", re.IGNORECASE), "EUR"),
)


def parse_price(text: str | None, *, default_currency: Currency | None = None) -> tuple[Decimal, Currency | None] | None:
    """"290 000 zł" -> (290000, PLN), "68 800 €" -> (68800, EUR); a bare number (from a page's
    JSON) takes `default_currency`. No number ("Zapytaj o cenę", "cena do negocjacji") -> None."""
    if not text:
        return None
    amount = parse_decimal(text)
    if amount is None or amount <= 0:
        return None
    currency = next((code for pattern, code in _CURRENCIES if pattern.search(text)), default_currency)
    return amount, currency


_AREA = re.compile(
    r"(?P<number>\d[\d\s  .,]*?)\s*(?P<unit>ha|m²|m2|m\^2|mkw|ar(?:y|ów|a)?|a)(?![a-ząćęłńóśźż0-9])",
    re.IGNORECASE,
)
_AREA_UNITS = {"ha": Decimal(10000), "a": Decimal(100), "ar": Decimal(100)}


def parse_area_m2(text: str | None) -> Decimal | None:
    """"870 m²" -> 870, "0,87 ha" -> 8700, "8,7 a" (ares) -> 870; a bare number is m²."""
    if not text:
        return None
    if match := _AREA.search(text):
        number = parse_decimal(match["number"])
        unit = match["unit"].lower()
        factor = _AREA_UNITS.get("ar" if unit.startswith("ar") else unit, Decimal(1))
    else:
        number, factor = parse_decimal(text), Decimal(1)
    if number is None or number <= 0:
        return None
    area = number * factor
    return area.quantize(Decimal(1)) if area == area.to_integral_value() else area.quantize(CENT)


def price_per_m2(price: Decimal | None, area_m2: Decimal | None) -> Decimal | None:
    """Price over area, rounded to 0.01; None unless both are known."""
    if price is None or area_m2 is None or area_m2 <= 0:
        return None
    try:
        return (price / area_m2).quantize(CENT, rounding=ROUND_HALF_UP)
    except InvalidOperation:
        return None


# --- dates ------------------------------------------------------------------------------------

_MONTHS = {
    # Polish, genitive ("12 września 2026") and nominative.
    "stycznia": 1, "styczeń": 1, "lutego": 2, "luty": 2, "marca": 3, "marzec": 3, "kwietnia": 4,
    "kwiecień": 4, "maja": 5, "maj": 5, "czerwca": 6, "czerwiec": 6, "lipca": 7, "lipiec": 7,
    "sierpnia": 8, "sierpień": 8, "września": 9, "wrzesień": 9, "października": 10,
    "październik": 10, "listopada": 11, "listopad": 11, "grudnia": 12, "grudzień": 12,
    # Slovak, genitive.
    "januára": 1, "februára": 2, "apríla": 4, "mája": 5, "júna": 6, "júla": 7,
    "augusta": 8, "septembra": 9, "októbra": 10, "novembra": 11, "decembra": 12,
}  # fmt: skip
_ISO_DATE = re.compile(r"(\d{4})-(\d{2})-(\d{2})")
_DOTTED_DATE = re.compile(r"(\d{1,2})\.(\d{1,2})\.(\d{4})")
_WORDED_DATE = re.compile(r"(\d{1,2})\s+([a-ząćęłńóśźżáäéíóôúýčďľňŕšťž]+)\s+(\d{4})", re.IGNORECASE)
_TODAY = re.compile(r"\b(dzisiaj|dziś|dnes)\b", re.IGNORECASE)
_YESTERDAY = re.compile(r"\b(wczoraj|včera)\b", re.IGNORECASE)


def parse_date(text: str | None, *, today: date) -> date | None:
    """"2026-09-12", "12.09.2026", "dodano 12 września 2026", "Dzisiaj o 08:14", "wczoraj" -> date."""
    if not text:
        return None
    try:
        if match := _ISO_DATE.search(text):
            return date(int(match[1]), int(match[2]), int(match[3]))
        if match := _DOTTED_DATE.search(text):
            return date(int(match[3]), int(match[2]), int(match[1]))
        if (match := _WORDED_DATE.search(text)) and (month := _MONTHS.get(match[2].lower())):
            return date(int(match[3]), month, int(match[1]))
    except ValueError:
        return None
    if _TODAY.search(text):
        return today
    if _YESTERDAY.search(text):
        return today - timedelta(days=1)
    return None


# --- sellers and contact data -----------------------------------------------------------------

_PRIVATE = re.compile(r"prywatn|bez pośrednik|właściciel|súkromn|private", re.IGNORECASE)
_BUSINESS = re.compile(
    r"biur|agencj|agency|pośredni|deweloper|firm|realitn|kancelári|makl|agent|developer|business", re.IGNORECASE
)


def private_seller(text: str | None) -> bool | None:
    """True for a private person, False for an agency or developer, None if the text doesn't say."""
    if not text:
        return None
    if _PRIVATE.search(text):
        return True
    if _BUSINESS.search(text):
        return False
    return None


# Contact data never reaches the model: a param named like one is dropped, and phone numbers and
# e-mail addresses inside other texts are cut out.
_CONTACT_NAME = re.compile(
    r"telefon|tel\.|e-?mail|kontakt|imi[eę]|nazwisk|meno|priezvisk|makl[eé]r|agent|źródło|zdroj|"
    r"sprzedawc|predajc|ogłoszeniodawca$",
    re.IGNORECASE,
)
# 9+ digits, single spaces or hyphens between them; not part of a longer or decimal number.
_PHONE = re.compile(r"(?<!\d)(?<!\d[,.])\+?\d(?:[\s-]?\d){8,}(?!\d|[,.]\d)")
_EMAIL = re.compile(r"[\w.+-]+@[\w-]+(?:\.[\w-]+)+")
CONTACT_REMOVED = "[contact removed]"


def without_contacts(text: str) -> str:
    return _EMAIL.sub(CONTACT_REMOVED, _PHONE.sub(CONTACT_REMOVED, text))


# --- raw offers into listings -----------------------------------------------------------------


class RawListing(BaseModel):
    """One offer as a portal's `extract_js` returns it: only the allowlisted fields, as the page
    shows them (texts) or as its embedded JSON holds them (numbers, ISO dates)."""

    title: str = ""
    href: str = ""
    location: str | None = None
    price: str | None = None
    currency: str | None = None
    area: str | None = None
    price_per_m2: str | None = None
    plot_type: str | None = None
    listed: str | None = None
    updated: str | None = None
    # e.g. "Prywatne", "biuro nieruchomości", "private"; mapped by `private_seller`.
    seller: str | None = None
    sponsored: bool = False


class RawPage(BaseModel):
    """What a portal's `extract_js` returns for a results page."""

    entries: list[dict] = []
    # The locality the page says it shows (heading, breadcrumbs, the page's own JSON).
    heading: str | None = None
    # The portal's own count of matching offers, as shown ("5 ogłoszeń") or as a number.
    count: str | None = None
    # The page's own "nothing found" state, where visible text can't tell it apart.
    no_results: bool = False


class RawDetails(BaseModel):
    """What a portal's `details_js` returns for an offer page."""

    offer: dict | None = None
    params: list[dict] = []
    # The page's own "this offer has ended" state, e.g. a status in its JSON.
    inactive: bool = False


class RawParam(BaseModel):
    name: str = ""
    value: str = ""


def raw_page(raw: object) -> RawPage | None:
    try:
        return RawPage.model_validate(raw)
    except ValidationError:
        logger.debug("Unexpected raw page: %.300r", raw)
        return None


def raw_details(raw: object) -> RawDetails | None:
    try:
        return RawDetails.model_validate(raw)
    except ValidationError:
        logger.debug("Unexpected raw details: %.300r", raw)
        return None


def raw_listings(raw: list[dict]) -> Iterator[RawListing]:
    """`raw` parsed into `RawListing`s; entries of an unexpected shape are skipped, not fatal."""
    for item in raw:
        try:
            yield RawListing.model_validate(item)
        except ValidationError:
            logger.debug("Skipping a malformed raw listing: %.200r", item)


def parse_count(text: str | None) -> int | None:
    number = parse_decimal(text) if text else None
    return int(number) if number is not None else None


def build_listing(
    entry: RawListing,
    url: str,
    *,
    rank: int,
    currency: Currency,
    today: date,
) -> Listing | None:
    """One clean `Listing`: every text screened and cut to size, numbers parsed. None without a title.

    An entry whose texts look like instructions aimed at the model is kept but withheld: no texts,
    URL without its query string, so the model can name the page without reading what it says.
    """
    screened = screen(
        title=without_contacts(entry.title),
        location=without_contacts(entry.location or ""),
        plot_type=_known(entry.plot_type),
    )
    if not screened.texts["title"]:
        return None
    parsed_price = parse_price(entry.price, default_currency=_currency(entry.currency) or currency)
    price, price_currency = parsed_price if parsed_price else (None, None)
    area = parse_area_m2(entry.area)
    per_m2 = parse_price(entry.price_per_m2)
    flags = list(screened.flags)
    withheld = screened.withheld
    if entry.sponsored:
        flags.append(SPONSORED_FLAG)
    if withheld:
        return Listing(rank=rank, title="", url=without_query(url), flags=flags, withheld=True)
    return Listing(
        rank=rank,
        title=screened.texts["title"][:TITLE_MAX_CHARS],
        url=url,
        location=screened.texts["location"][:LISTING_LOCATION_MAX_CHARS] or None,
        price=price,
        currency=price_currency,
        area_m2=area,
        price_per_m2=per_m2[0].quantize(CENT, rounding=ROUND_HALF_UP) if per_m2 else price_per_m2(price, area),
        plot_type=screened.texts["plot_type"][:PLOT_TYPE_MAX_CHARS] or None,
        listed_at=parse_date(entry.listed, today=today),
        updated_at=parse_date(entry.updated, today=today),
        private_seller=private_seller(entry.seller),
        flags=flags,
    )


def build_listings(
    raw: list[dict],
    request: ListingSearchRequest,
    resolve_href: Callable[[str], str | None],
    *,
    currency: Currency,
    today: date,
) -> list[Listing]:
    """Offers with an offer URL of the portal, deduplicated by URL, ranked from 1, at most
    `request.max_results`. An offer priced above `max_price` or smaller than `min_area_m2` is
    dropped, whether or not the portal applied the filter; one without a price or area is kept."""
    listings: list[Listing] = []
    seen: set[str] = set()
    for entry in raw_listings(raw):
        if len(listings) >= request.max_results:
            break
        url = resolve_href(entry.href)
        if url is None or url in seen:
            continue
        listing = build_listing(entry, url, rank=len(listings) + 1, currency=currency, today=today)
        if listing is None or not _within_filters(listing, request):
            continue
        seen.add(url)
        listings.append(listing)
    return listings


def _within_filters(listing: Listing, request: ListingSearchRequest) -> bool:
    if request.max_price is not None and listing.price is not None and listing.price > request.max_price:
        return False
    if request.min_area_m2 is not None and listing.area_m2 is not None and listing.area_m2 < request.min_area_m2:
        return False
    return True


def build_params(raw: Iterable[dict]) -> tuple[list[ListingParam], tuple[str, ...]]:
    """The offer's parameter table: at most `MAX_PARAMS` rows, values cut to `PARAM_VALUE_MAX_CHARS`,
    duplicates and anything naming contact data dropped. Also every injection signal seen: with
    any, the caller withholds the whole offer."""
    params: list[ListingParam] = []
    flags: list[str] = []
    names: set[str] = set()
    for item in raw:
        try:
            row = RawParam.model_validate(item)
        except ValidationError:
            continue
        screened = screen(name=row.name, value=without_contacts(row.value))
        flags.extend(flag for flag in screened.flags if flag not in flags)
        name, value = screened.texts["name"].rstrip(":").strip(), screened.texts["value"]
        if not name or not value or name.lower() in names or _CONTACT_NAME.search(name):
            continue
        names.add(name.lower())
        params.append(ListingParam(name=name[:PARAM_VALUE_MAX_CHARS], value=value[:PARAM_VALUE_MAX_CHARS]))
        if len(params) >= MAX_PARAMS:
            break
    return params, tuple(flags)


def _known(text: str | None) -> str:
    """`text`, or "" where the portal shows a placeholder ("-", "brak informacji") for no value."""
    if not text or _PLACEHOLDER.fullmatch(text.strip()):
        return ""
    return text


_PLACEHOLDER = re.compile(r"[-–—?]*|brak( informacji| danych)?|nie podano|neuvedené", re.IGNORECASE)


def _currency(text: str | None) -> Currency | None:
    if not text:
        return None
    return next((code for pattern, code in _CURRENCIES if pattern.search(text)), None)
