from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from contracts.auth import is_valid_bearer
from contracts.listings import (
    LISTING_DETAILS_PATH,
    LISTING_SEARCH_PATH,
    PORTALS_PATH,
    ListingDetailsRequest,
    ListingDetailsResponse,
    ListingSearchRequest,
    ListingSearchResponse,
    PortalsResponse,
)
from contracts.web_agent import (
    SEARCH_PATH,
    SITE_SEARCH_PATH,
    SITES_PATH,
    SearchResponse,
    SiteSearchRequest,
    SitesResponse,
    WebSearchRequest,
)
from fastapi import Depends, FastAPI, HTTPException, Request, status

from web_agent.config import settings
from web_agent.engines import get_engine
from web_agent.models import SearchQuery
from web_agent.portals import PORTAL_NAMES, ListingPortal, get_portal
from web_agent.services import build_services
from web_agent.sites import SITE_NAMES, get_site


def _require_bearer_auth(request: Request) -> None:
    """Same bearer convention as llm-proxy: `INTERNAL_PROXY_TOKEN`, checked in constant time."""
    if not is_valid_bearer(request.headers.get("authorization"), settings.internal_proxy_token):
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "Invalid or missing API key")


def _require_web_search() -> None:
    """Off in the sites-only instance (`site-agent`): web searches never reach its browser."""
    if settings.sites_only:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Not Found")


def _require_sites() -> None:
    """Off by default (the compose instance): shop sites never reach the headless browser."""
    if not settings.serves_sites:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Not Found")


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    if not settings.internal_proxy_token:
        raise RuntimeError("INTERNAL_PROXY_TOKEN is not set; refusing to serve an unauthenticated browser")
    app.state.engine = None if settings.sites_only else get_engine(settings.engine, searxng_url=settings.searxng_url)
    services = build_services(settings)
    app.state.search_service = services.search
    app.state.listing_service = services.listings
    await services.manager.start()
    yield
    await services.manager.close()


def _portal(name: str) -> ListingPortal:
    """The portal by name; an unknown one is the caller's mistake (422), like an unknown site."""
    try:
        return get_portal(name)
    except ValueError as exc:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_CONTENT, str(exc)) from exc


def create_app() -> FastAPI:
    app = FastAPI(title="local-agent web-agent", lifespan=lifespan)

    @app.get("/health")
    async def health() -> dict:
        return {"status": "ok"}

    @app.post(SEARCH_PATH, dependencies=[Depends(_require_web_search), Depends(_require_bearer_auth)])
    async def search(body: WebSearchRequest, request: Request) -> SearchResponse:
        """A blocked or failed search is still HTTP 200: the outcome is in `status`, for the model."""
        query = SearchQuery.model_validate(body.model_dump())
        return await request.app.state.search_service.search(query, request.app.state.engine)

    @app.post(SITE_SEARCH_PATH, dependencies=[Depends(_require_sites), Depends(_require_bearer_auth)])
    async def site_search(body: SiteSearchRequest, request: Request) -> SearchResponse:
        """The site's own search page; same outcome convention as `/v1/search`."""
        try:
            site = get_site(body.site)
        except ValueError as exc:
            raise HTTPException(status.HTTP_422_UNPROCESSABLE_CONTENT, str(exc)) from exc
        query = SearchQuery.model_validate(body.model_dump(exclude={"site"}))
        return await request.app.state.search_service.search(query, site)

    @app.get(SITES_PATH, dependencies=[Depends(_require_sites), Depends(_require_bearer_auth)])
    async def sites() -> SitesResponse:
        return SitesResponse(sites=list(SITE_NAMES))

    # Real-estate portals run in the site browser too (`site-agent`), behind the same switch.
    @app.post(LISTING_SEARCH_PATH, dependencies=[Depends(_require_sites), Depends(_require_bearer_auth)])
    async def listing_search(body: ListingSearchRequest, request: Request) -> ListingSearchResponse:
        """One portal, one locality; same outcome convention as `/v1/search`."""
        return await request.app.state.listing_service.listing_search(body, _portal(body.portal))

    @app.post(LISTING_DETAILS_PATH, dependencies=[Depends(_require_sites), Depends(_require_bearer_auth)])
    async def listing_details(body: ListingDetailsRequest, request: Request) -> ListingDetailsResponse:
        """One offer page; a URL that isn't an offer page of the portal never reaches the browser."""
        portal = _portal(body.portal)
        if portal.offer_url(body.url) is None:
            raise HTTPException(status.HTTP_422_UNPROCESSABLE_CONTENT, f"Not an offer page of {portal.name}")
        return await request.app.state.listing_service.listing_details(body, portal)

    @app.get(PORTALS_PATH, dependencies=[Depends(_require_sites), Depends(_require_bearer_auth)])
    async def portals() -> PortalsResponse:
        return PortalsResponse(portals=list(PORTAL_NAMES))

    return app


app = create_app()


if __name__ == "__main__":
    import uvicorn

    uvicorn.run("web_agent.main:app", host="0.0.0.0", port=8080)
