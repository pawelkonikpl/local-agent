"""The Solver CLI: `web-agent search "<query>"` and `web-agent site <site> "<query>"` -- the manual
way to exercise the browser path."""

import argparse
import asyncio
import sys
from pathlib import Path
from typing import get_args

from pydantic import ValidationError

from web_agent.browser import BrowserSession
from web_agent.config import settings
from web_agent.engines import ENGINE_NAMES, SearchEngine, get_engine
from web_agent.formatting import format_text
from web_agent.models import SearchQuery, SearchResponse, SortOrder
from web_agent.search import PageHook, build_search_service
from web_agent.sites import SITE_NAMES, get_site

EXIT_CODES = {"ok": 0, "no_results": 0, "blocked": 2, "error": 1}


def _parser() -> argparse.ArgumentParser:
    browser = argparse.ArgumentParser(add_help=False)
    browser.add_argument("--max-results", type=int, default=5)
    browser.add_argument("--json", action="store_true", help="Print the full SearchResponse as JSON")
    browser.add_argument("--headful", action="store_true", help="Show the browser window")
    browser.add_argument("--cdp-url", help="Attach to a running Chrome, e.g. http://127.0.0.1:9222")
    browser.add_argument("--screenshot", type=Path, help="Save a PNG of the results page here")
    browser.add_argument("--save-html", type=Path, help="Save the results page's HTML here (test fixtures)")

    parser = argparse.ArgumentParser(prog="web-agent")
    commands = parser.add_subparsers(dest="command", required=True)
    search = commands.add_parser("search", parents=[browser], help="Search the web and print the results")
    search.add_argument("query")
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
    site.add_argument("--sort", choices=get_args(SortOrder), default="relevance")
    return parser


def _query_and_engine(args: argparse.Namespace) -> tuple[SearchQuery, SearchEngine]:
    if args.command == "site":
        return SearchQuery(query=args.query, max_results=args.max_results, sort=args.sort), get_site(args.site)
    query = SearchQuery(query=args.query, max_results=args.max_results, region=args.region)
    return query, get_engine(args.engine, searxng_url=args.searxng_url)


async def _search(args: argparse.Namespace, query: SearchQuery, engine: SearchEngine) -> SearchResponse:
    overrides: dict = {"headless": False} if args.headful else {}
    if args.cdp_url:
        overrides["cdp_url"] = args.cdp_url
        # A site in an attached browser's profile: a tab there, so a passed bot check carries over.
        if args.command == "site":
            overrides["reuse_default_context"] = True
    manager, service = build_search_service(settings.model_copy(update=overrides))

    hooks: list[PageHook] = []
    if args.screenshot:

        async def save_screenshot(session: BrowserSession) -> None:
            args.screenshot.write_bytes(await session.screenshot())

        hooks.append(save_screenshot)
    if args.save_html:

        async def save_html(session: BrowserSession) -> None:
            args.save_html.write_text(await session.html(), encoding="utf-8")

        hooks.append(save_html)

    async def on_page(session: BrowserSession) -> None:
        for hook in hooks:
            await hook(session)

    try:
        return await service.search(query, engine, on_page=on_page if hooks else None)
    finally:
        await manager.close()


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    try:
        query, engine = _query_and_engine(args)
    except (ValidationError, ValueError) as exc:
        print(exc, file=sys.stderr)
        return 1
    response = asyncio.run(_search(args, query, engine))
    print(response.model_dump_json(indent=2) if args.json else format_text(response))
    return EXIT_CODES[response.status]


if __name__ == "__main__":
    sys.exit(main())
