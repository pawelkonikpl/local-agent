import secrets
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from fastapi import Depends, FastAPI, HTTPException, Request, status
from pydantic import BaseModel

from web_agent.config import settings
from web_agent.engines import get_engine
from web_agent.models import SearchQuery, SearchResponse, SiteSearchQuery
from web_agent.search import build_search_service
from web_agent.sites import SITE_NAMES, get_site


class SitesResponse(BaseModel):
    sites: list[str]


def _require_bearer_auth(request: Request) -> None:
    """Same bearer convention as llm-proxy: `INTERNAL_PROXY_TOKEN`, checked in constant time."""
    expected = settings.internal_proxy_token
    scheme, _, token = request.headers.get("authorization", "").partition(" ")
    if not expected or scheme.lower() != "bearer" or not secrets.compare_digest(token, expected):
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
    manager, app.state.search_service = build_search_service(settings)
    await manager.start()
    yield
    await manager.close()


def create_app() -> FastAPI:
    app = FastAPI(title="local-agent web-agent", lifespan=lifespan)

    @app.get("/health")
    async def health() -> dict:
        return {"status": "ok"}

    @app.post("/v1/search", dependencies=[Depends(_require_web_search), Depends(_require_bearer_auth)])
    async def search(query: SearchQuery, request: Request) -> SearchResponse:
        """A blocked or failed search is still HTTP 200: the outcome is in `status`, for the model."""
        return await request.app.state.search_service.search(query, request.app.state.engine)

    @app.post("/v1/site-search", dependencies=[Depends(_require_sites), Depends(_require_bearer_auth)])
    async def site_search(body: SiteSearchQuery, request: Request) -> SearchResponse:
        """The site's own search page; same outcome convention as `/v1/search`."""
        try:
            site = get_site(body.site)
        except ValueError as exc:
            raise HTTPException(status.HTTP_422_UNPROCESSABLE_CONTENT, str(exc)) from exc
        query = SearchQuery(query=body.query, max_results=body.max_results, sort=body.sort)
        return await request.app.state.search_service.search(query, site)

    @app.get("/v1/sites", dependencies=[Depends(_require_sites), Depends(_require_bearer_auth)])
    async def sites() -> SitesResponse:
        return SitesResponse(sites=list(SITE_NAMES))

    return app


app = create_app()


if __name__ == "__main__":
    import uvicorn

    uvicorn.run("web_agent.main:app", host="0.0.0.0", port=8080)
