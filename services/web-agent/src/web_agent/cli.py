"""The Solver CLI -- the manual way to exercise the browser path:

- `web-agent search "<query>"` and `web-agent site <site> "<query>"`: web and shop searches;
- `web-agent listings <portal> "<locality>"` and `web-agent listing <portal> <offer url>`: plots on
  a real-estate portal (also how their test fixtures are saved, with `--save-html`).
"""

import argparse
import asyncio
import sys
from pathlib import Path
from typing import get_args

from pydantic import ValidationError

from web_agent.browser import BrowserSession
from web_agent.config import settings
from web_agent.engines import ENGINE_NAMES, get_engine
from web_agent.formatting import format_details, format_listings, format_text
from web_agent.models import (
    ListingDetailsRequest,
    ListingDetailsResponse,
    ListingSearchRequest,
    ListingSearchResponse,
    ListingSort,
    SearchQuery,
    SearchResponse,
    SortOrder,
)
from web_agent.portals import PORTAL_NAMES, get_portal
from web_agent.search import PageHook
from web_agent.services import Services, build_services
from web_agent.sites import SITE_NAMES, get_site

EXIT_CODES = {"ok": 0, "no_results": 0, "unknown_location": 0, "inactive": 0, "blocked": 2, "error": 1}
# Commands that run in the site browser's profile when attached to it.
SITE_COMMANDS = frozenset({"site", "listings", "listing"})

Response = SearchResponse | ListingSearchResponse | ListingDetailsResponse


def _parser() -> argparse.ArgumentParser:
    browser = argparse.ArgumentParser(add_help=False)
    browser.add_argument("--json", action="store_true", help="Print the full response as JSON")
    browser.add_argument("--headful", action="store_true", help="Show the browser window")
    browser.add_argument("--cdp-url", help="Attach to a running Chrome, e.g. http://127.0.0.1:9222")
    browser.add_argument("--screenshot", type=Path, help="Save a PNG of the page here")
    browser.add_argument("--save-html", type=Path, help="Save the page's HTML here (test fixtures)")

    parser = argparse.ArgumentParser(prog="web-agent")
    commands = parser.add_subparsers(dest="command", required=True)
    search = commands.add_parser("search", parents=[browser], help="Search the web and print the results")
    search.add_argument("query")
    search.add_argument("--max-results", type=int, default=5)
    search.add_argument("--region", help="e.g. pl-pl, us-en, wt-wt (default: WEB_AGENT_DEFAULT_REGION)")
    search.add_argument("--engine", choices=ENGINE_NAMES, default=settings.engine)
    search.add_argument("--searxng-url", default=settings.searxng_url, help="e.g. http://localhost:8092")
    site = commands.add_parser(
        "site",
        parents=[browser],
        help="Search inside a site through its own search page (with --cdp-url: in the site browser)",
    )
    site.add_argument("site", choices=SITE_NAMES)
    site.add_argument("query")
    site.add_argument("--max-results", type=int, default=5)
    site.add_argument("--sort", choices=get_args(SortOrder), default="relevance")
    listings = commands.add_parser(
        "listings", parents=[browser], help="Plots for sale in one locality on a real-estate portal"
    )
    listings.add_argument("portal", choices=PORTAL_NAMES)
    listings.add_argument("location", help='e.g. "Białka Tatrzańska", "Oravská Lesná"')
    listings.add_argument("--max-price", type=int, help="In the portal's currency (PLN or EUR)")
    listings.add_argument("--min-area", type=int, help="In m²")
    listings.add_argument("--max-results", type=int, default=10)
    listings.add_argument("--sort", choices=get_args(ListingSort), default="newest")
    listing = commands.add_parser("listing", parents=[browser], help="One offer page of a real-estate portal")
    listing.add_argument("portal", choices=PORTAL_NAMES)
    listing.add_argument("url")
    return parser


def _page_hook(args: argparse.Namespace) -> PageHook | None:
    hooks: list[PageHook] = []
    if args.screenshot:

        async def save_screenshot(session: BrowserSession) -> None:
            args.screenshot.write_bytes(await session.screenshot())

        hooks.append(save_screenshot)
    if args.save_html:

        async def save_html(session: BrowserSession) -> None:
            args.save_html.write_text(await session.html(), encoding="utf-8")

        hooks.append(save_html)
    if not hooks:
        return None

    async def on_page(session: BrowserSession) -> None:
        for hook in hooks:
            await hook(session)

    return on_page


async def _run(args: argparse.Namespace, services: Services) -> Response:
    on_page = _page_hook(args)
    match args.command:
        case "site":
            query = SearchQuery(query=args.query, max_results=args.max_results, sort=args.sort)
            return await services.search.search(query, get_site(args.site), on_page=on_page)
        case "listings":
            request = ListingSearchRequest(
                portal=args.portal,
                location=args.location,
                max_price=args.max_price,
                min_area_m2=args.min_area,
                max_results=args.max_results,
                sort=args.sort,
            )
            return await services.listings.listing_search(request, get_portal(args.portal), on_page=on_page)
        case "listing":
            portal = get_portal(args.portal)
            if portal.offer_url(args.url) is None:
                raise ValueError(f"Not an offer page of {portal.name}: {args.url}")
            request = ListingDetailsRequest(portal=args.portal, url=args.url)
            return await services.listings.listing_details(request, portal, on_page=on_page)
        case _:
            query = SearchQuery(query=args.query, max_results=args.max_results, region=args.region)
            engine = get_engine(args.engine, searxng_url=args.searxng_url)
            return await services.search.search(query, engine, on_page=on_page)


async def _main(args: argparse.Namespace) -> Response:
    overrides: dict = {"headless": False} if args.headful else {}
    if args.cdp_url:
        overrides["cdp_url"] = args.cdp_url
        # A site in an attached browser's profile: a tab there, so a passed bot check carries over.
        if args.command in SITE_COMMANDS:
            overrides["reuse_default_context"] = True
    services = build_services(settings.model_copy(update=overrides))
    try:
        return await _run(args, services)
    finally:
        await services.manager.close()


def _format(response: Response) -> str:
    match response:
        case SearchResponse():
            return format_text(response)
        case ListingDetailsResponse():
            return format_details(response)
        case ListingSearchResponse():
            return format_listings(response)


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    try:
        response = asyncio.run(_main(args))
    except (ValidationError, ValueError) as exc:
        print(exc, file=sys.stderr)
        return 1
    print(response.model_dump_json(indent=2) if args.json else _format(response))
    return EXIT_CODES[response.status]


if __name__ == "__main__":
    sys.exit(main())
