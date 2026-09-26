from collections.abc import AsyncGenerator

import pytest
import pytest_asyncio
from httpx import ASGITransport, AsyncClient

from web_agent import main
from web_agent.engines.duckduckgo import DuckDuckGoEngine
from web_agent.models import SearchQuery, SearchResponse, SearchResult

TOKEN = "test-token"


class FakeSearchService:
    def __init__(self) -> None:
        self.queries: list[SearchQuery] = []

    async def search(self, query: SearchQuery, engine) -> SearchResponse:
        self.queries.append(query)
        result = SearchResult(rank=1, title="T", url="https://example.com", snippet="s", domain="example.com")
        return SearchResponse(status="ok", engine=engine.name, query=query.query, results=[result], elapsed_ms=3)


@pytest.fixture
def service(monkeypatch: pytest.MonkeyPatch) -> FakeSearchService:
    monkeypatch.setattr(main.settings, "internal_proxy_token", TOKEN)
    return FakeSearchService()


@pytest_asyncio.fixture
async def client(service: FakeSearchService) -> AsyncGenerator[AsyncClient, None]:
    # ASGITransport doesn't run lifespan, so no browser starts; the fake stands in for it.
    app = main.create_app()
    app.state.search_service = service
    app.state.engine = DuckDuckGoEngine()
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as ac:
        yield ac


async def test_health(client: AsyncClient) -> None:
    response = await client.get("/health")

    assert response.json() == {"status": "ok"}


@pytest.mark.parametrize("authorization", [None, "Bearer wrong", f"Basic {TOKEN}"])
async def test_search_requires_token(client: AsyncClient, authorization: str | None) -> None:
    headers = {"Authorization": authorization} if authorization else {}

    response = await client.post("/v1/search", json={"query": "x"}, headers=headers)

    assert response.status_code == 401


async def test_search_rejects_empty_query(client: AsyncClient) -> None:
    response = await client.post("/v1/search", json={"query": ""}, headers={"Authorization": f"Bearer {TOKEN}"})

    assert response.status_code == 422


async def test_search_returns_search_response(client: AsyncClient, service: FakeSearchService) -> None:
    response = await client.post(
        "/v1/search", json={"query": " podman ", "max_results": 3}, headers={"Authorization": f"Bearer {TOKEN}"}
    )

    assert response.status_code == 200
    body = SearchResponse.model_validate(response.json())
    assert (body.status, body.engine, body.query) == ("ok", "duckduckgo", "podman")
    assert service.queries[0].max_results == 3


async def test_search_rejects_everything_when_token_unset(monkeypatch: pytest.MonkeyPatch, client: AsyncClient) -> None:
    monkeypatch.setattr(main.settings, "internal_proxy_token", None)

    response = await client.post("/v1/search", json={"query": "x"}, headers={"Authorization": "Bearer "})

    assert response.status_code == 401
