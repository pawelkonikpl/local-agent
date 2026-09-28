import httpx
import httpx2
import openai
from httpx import AsyncClient

from llm_proxy.config import settings
from llm_proxy.main import app, get_anthropic_client, get_openai_client

_AUTH = {"Authorization": f"Bearer {settings.internal_proxy_token}"}


def _effort(*levels: str) -> dict:
    all_levels = ("low", "medium", "high", "xhigh", "max")
    return {"supported": bool(levels), **{level: {"supported": level in levels} for level in all_levels}}


def _anthropic_model(model_id: str, effort: dict) -> dict:
    return {
        "type": "model",
        "id": model_id,
        "display_name": model_id,
        "created_at": "2026-01-01T00:00:00Z",
        "capabilities": {"image_input": {"supported": True}, "effort": effort},
    }


def _mock_anthropic_client(pages: list[dict], seen: list[httpx.Request]) -> httpx.AsyncClient:
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/v1/models"
        seen.append(request)
        return httpx.Response(200, json=pages[len(seen) - 1])

    return httpx.AsyncClient(transport=httpx.MockTransport(handler), base_url="https://mock-anthropic.test")


def _mock_openai_client(response: httpx2.Response) -> openai.AsyncOpenAI:
    return openai.AsyncOpenAI(
        api_key="test-key",
        max_retries=0,
        http_client=httpx2.AsyncClient(transport=httpx2.MockTransport(lambda request: response)),
    )


def _openai_models(*ids_and_created: tuple[str, int]) -> httpx2.Response:
    data = [{"id": model_id, "object": "model", "created": created, "owned_by": "openai"} for model_id, created in ids_and_created]
    return httpx2.Response(200, json={"object": "list", "data": data})


_ONE_PAGE = {
    "data": [
        _anthropic_model("claude-sonnet-5", _effort("low", "medium", "high", "xhigh", "max")),
        _anthropic_model("claude-haiku-4-5-20251001", _effort()),
    ],
    "has_more": False,
    "first_id": "claude-sonnet-5",
    "last_id": "claude-haiku-4-5-20251001",
}


async def test_list_models_without_valid_token_returns_401(client: AsyncClient) -> None:
    response = await client.get("/v1/models", headers={"Authorization": "Bearer wrong-token"})
    assert response.status_code == 401


async def test_list_models_reads_anthropic_models_and_their_effort_levels(client: AsyncClient) -> None:
    seen: list[httpx.Request] = []
    app.dependency_overrides[get_anthropic_client] = lambda: _mock_anthropic_client([_ONE_PAGE], seen)

    response = await client.get("/v1/models", headers=_AUTH)

    assert response.status_code == 200
    assert response.json()["models"] == [
        {"id": "claude-sonnet-5", "reasoning_efforts": ["low", "medium", "high", "xhigh", "max"]},
        {"id": "claude-haiku-4-5-20251001", "reasoning_efforts": []},
    ]
    assert seen[0].headers["x-api-key"] == settings.anthropic_api_key


async def test_list_models_follows_anthropic_pagination(client: AsyncClient) -> None:
    first = {**_ONE_PAGE, "data": _ONE_PAGE["data"][:1], "has_more": True, "last_id": "claude-sonnet-5"}
    second = {**_ONE_PAGE, "data": _ONE_PAGE["data"][1:], "has_more": False}
    seen: list[httpx.Request] = []
    app.dependency_overrides[get_anthropic_client] = lambda: _mock_anthropic_client([first, second], seen)

    response = await client.get("/v1/models", headers=_AUTH)

    assert [model["id"] for model in response.json()["models"]] == ["claude-sonnet-5", "claude-haiku-4-5-20251001"]
    assert seen[1].url.params["after_id"] == "claude-sonnet-5"


async def test_list_models_adds_openai_models_matching_the_pattern(client: AsyncClient) -> None:
    seen: list[httpx.Request] = []
    app.dependency_overrides[get_anthropic_client] = lambda: _mock_anthropic_client([_ONE_PAGE], seen)
    app.dependency_overrides[get_openai_client] = lambda: _mock_openai_client(
        _openai_models(
            ("gpt-6-sol", 20),
            ("gpt-6-sol-2026-05-01", 21),
            ("gpt-6-luna", 30),
            ("gpt-4o", 10),
            ("text-embedding-3-large", 5),
        )
    )

    response = await client.get("/v1/models", headers=_AUTH)

    openai_models = response.json()["models"][2:]
    assert openai_models == [
        {"id": "gpt-6-luna", "reasoning_efforts": settings.openai_reasoning_efforts},
        {"id": "gpt-6-sol", "reasoning_efforts": settings.openai_reasoning_efforts},
    ]


async def test_list_models_leaves_out_a_provider_that_fails(client: AsyncClient) -> None:
    seen: list[httpx.Request] = []
    app.dependency_overrides[get_anthropic_client] = lambda: _mock_anthropic_client([_ONE_PAGE, _ONE_PAGE], seen)
    app.dependency_overrides[get_openai_client] = lambda: _mock_openai_client(
        httpx2.Response(500, json={"error": {"message": "boom"}})
    )

    first = await client.get("/v1/models", headers=_AUTH)
    await client.get("/v1/models", headers=_AUTH)

    assert first.status_code == 200
    assert [model["id"] for model in first.json()["models"]] == ["claude-sonnet-5", "claude-haiku-4-5-20251001"]
    # A partial answer isn't cached: the second request asked Anthropic again.
    assert len(seen) == 2


async def test_list_models_caches_the_providers_answer(client: AsyncClient) -> None:
    seen: list[httpx.Request] = []
    app.dependency_overrides[get_anthropic_client] = lambda: _mock_anthropic_client([_ONE_PAGE], seen)

    await client.get("/v1/models", headers=_AUTH)
    second = await client.get("/v1/models", headers=_AUTH)

    assert second.status_code == 200
    assert len(seen) == 1


async def test_list_models_returns_502_when_no_provider_answers(client: AsyncClient) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(529, json={"type": "error"})

    app.dependency_overrides[get_anthropic_client] = lambda: httpx.AsyncClient(
        transport=httpx.MockTransport(handler), base_url="https://mock-anthropic.test"
    )

    response = await client.get("/v1/models", headers=_AUTH)

    assert response.status_code == 502
