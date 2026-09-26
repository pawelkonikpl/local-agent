"""The Solver CLI: `web-agent search "<query>"` -- the manual way to exercise the browser path."""

import argparse
import asyncio
import sys
from pathlib import Path

from pydantic import ValidationError

from web_agent.browser import BrowserSession
from web_agent.config import settings
from web_agent.engines import ENGINE_NAMES, SearchEngine, get_engine
from web_agent.formatting import format_text
from web_agent.models import SearchQuery, SearchResponse
from web_agent.search import build_search_service

EXIT_CODES = {"ok": 0, "no_results": 0, "blocked": 2, "error": 1}


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="web-agent")
    commands = parser.add_subparsers(dest="command", required=True)
    search = commands.add_parser("search", help="Search the web and print the results")
    search.add_argument("query")
    search.add_argument("--max-results", type=int, default=5)
    search.add_argument("--region", help="e.g. pl-pl, us-en, wt-wt (default: WEB_AGENT_DEFAULT_REGION)")
    search.add_argument("--engine", choices=ENGINE_NAMES, default=settings.engine)
    search.add_argument("--searxng-url", default=settings.searxng_url, help="e.g. http://localhost:8092")
    search.add_argument("--json", action="store_true", help="Print the full SearchResponse as JSON")
    search.add_argument("--headful", action="store_true", help="Show the browser window")
    search.add_argument("--cdp-url", help="Attach to a running Chrome, e.g. http://localhost:9222")
    search.add_argument("--screenshot", type=Path, help="Save a PNG of the results page here")
    return parser


async def _search(args: argparse.Namespace, query: SearchQuery, engine: SearchEngine) -> SearchResponse:
    overrides: dict = {"headless": False} if args.headful else {}
    if args.cdp_url:
        overrides["cdp_url"] = args.cdp_url
    manager, service = build_search_service(settings.model_copy(update=overrides))

    async def save_screenshot(session: BrowserSession) -> None:
        args.screenshot.write_bytes(await session.screenshot())

    try:
        return await service.search(query, engine, on_page=save_screenshot if args.screenshot else None)
    finally:
        await manager.close()


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    try:
        query = SearchQuery(query=args.query, max_results=args.max_results, region=args.region)
        engine = get_engine(args.engine, searxng_url=args.searxng_url)
    except (ValidationError, ValueError) as exc:
        print(exc, file=sys.stderr)
        return 1
    response = asyncio.run(_search(args, query, engine))
    print(response.model_dump_json(indent=2) if args.json else format_text(response))
    return EXIT_CODES[response.status]


if __name__ == "__main__":
    sys.exit(main())
