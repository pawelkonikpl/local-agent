"""Real-estate portals searched for plots (`plot_search`, `plot_details`), as `ListingPortal`s.

Adding a portal: a module here, an entry in `PORTALS`, its offer URL pattern in
`contracts.listings.OFFER_URL_PATTERNS`, its hosts in deploy/egress-proxy/allowlist.txt. Like
`sites/`, nothing here imports Playwright.
"""

from collections.abc import Callable

from web_agent.portals.adresowo import AdresowoPortal
from web_agent.portals.base import ListingPortal
from web_agent.portals.nehnutelnosti_sk import NehnutelnostiSkPortal
from web_agent.portals.nieruchomosci_online import NieruchomosciOnlinePortal
from web_agent.portals.olx import OlxPortal
from web_agent.portals.otodom import OtodomPortal

PORTALS: dict[str, Callable[[], ListingPortal]] = {
    NieruchomosciOnlinePortal.name: NieruchomosciOnlinePortal,
    NehnutelnostiSkPortal.name: NehnutelnostiSkPortal,
    OtodomPortal.name: OtodomPortal,
    OlxPortal.name: OlxPortal,
    AdresowoPortal.name: AdresowoPortal,
}
PORTAL_NAMES = tuple(PORTALS)


def get_portal(name: str) -> ListingPortal:
    factory = PORTALS.get(name)
    if factory is None:
        raise ValueError(f"Unknown portal: {name}")
    return factory()


__all__ = ["PORTALS", "PORTAL_NAMES", "ListingPortal", "get_portal"]
