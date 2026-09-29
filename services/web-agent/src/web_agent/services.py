from dataclasses import dataclass

from web_agent.browser import BrowserManager
from web_agent.config import Settings
from web_agent.listings import ListingService
from web_agent.search import SearchService, build_browser


@dataclass(frozen=True)
class Services:
    """Everything that drives this process's one browser, sharing its rate limiter."""

    manager: BrowserManager
    search: SearchService
    listings: ListingService


def build_services(settings: Settings) -> Services:
    manager, limiter = build_browser(settings)
    return Services(
        manager=manager,
        search=SearchService(
            manager, limiter, timeout_s=settings.search_timeout_s, default_region=settings.default_region
        ),
        listings=ListingService(manager, limiter, timeout_s=settings.search_timeout_s),
    )
