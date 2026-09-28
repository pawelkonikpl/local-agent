"""Sites searched through their own search page (`site_search`), as `SearchEngine`s.

Adding a site: a module here, an entry in `SITES`, its hosts in deploy/egress-proxy/allowlist.txt.
Like `engines/`, nothing here imports Playwright.
"""

from collections.abc import Callable

from web_agent.engines.base import SearchEngine
from web_agent.sites.allegro import AllegroSite

SITES: dict[str, Callable[[], SearchEngine]] = {AllegroSite.name: AllegroSite}
SITE_NAMES = tuple(SITES)


def get_site(name: str) -> SearchEngine:
    factory = SITES.get(name)
    if factory is None:
        raise ValueError(f"Unknown site: {name}")
    return factory()


__all__ = ["SITES", "SITE_NAMES", "get_site"]
