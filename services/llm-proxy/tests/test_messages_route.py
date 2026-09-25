import json
import uuid
from collections.abc import Callable
from datetime import date
from decimal import ROUND_HALF_UP, Decimal

import httpx
import httpx2
import openai
from httpx import AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession

from llm_proxy import metering
from llm_proxy.config import settings
from llm_proxy.main import app, get_anthropic_client, get_local_model_client, get_openai_client
from shared.db.models import ChatSession, UserUsageCounter, UserUsageLimit

_PERIOD_START = date.today().replace(day=1)


def _sse_body(text: str, input_tokens: int, output_tokens: int, model: str) -> bytes:
    events: list[tuple[str, dict]] = [
        (
            "message_start",
            {
                "type": "message_start",
                "message": {"id": "msg_1", "model": model, "usage": {"input_tokens": input_tokens, "output_tokens": 0}},
            },
        ),
        ("content_block_start", {"type": "content_block_start", "index": 0, "content_block": {"type": "text", "text": ""}}),
        (
            "content_block_delta",
            {"type": "content_block_delta", "index": 0, "delta": {"type": "text_delta", "text": text}},
        ),
        ("content_block_stop", {"type": "content_block_stop", "index": 0}),
        (
            "message_delta",
            {"type": "message_delta", "delta": {"stop_reason": "end_turn"}, "usage": {"output_tokens": output_tokens}},
        ),
        ("message_stop", {"type": "message_stop"}),
    ]
    lines: list[str] = []
    for event_type, data in events:
        lines.append(f"event: {event_type}")
        lines.append(f"data: {json.dumps(data)}")
        lines.append("")
    return ("\n".join(lines) + "\n").encode()


def _mock_anthropic_client(handler: Callable[[httpx.Request], httpx.Response]) -> httpx.AsyncClient:
    return httpx.AsyncClient(transport=httpx.MockTransport(handler), base_url="https://mock-anthropic.test")


def _openai_sse_body(text: str, prompt_tokens: int, completion_tokens: int, model: str) -> bytes:
    chunks: list[dict] = [
        {
            "id": "chatcmpl_1",
            "object": "chat.completion.chunk",
            "created": 1,
            "model": model,
            "choices": [{"index": 0, "delta": {"content": text}, "finish_reason": None}],
        },
        {
            "id": "chatcmpl_1",
            "object": "chat.completion.chunk",
            "created": 1,
            "model": model,
            "choices": [{"index": 0, "delta": {}, "finish_reason": "stop"}],
        },
        {
            "id": "chatcmpl_1",
            "object": "chat.completion.chunk",
            "created": 1,
            "model": model,
            "choices": [],
            "usage": {
                "prompt_tokens": prompt_tokens,
                "completion_tokens": completion_tokens,
                "total_tokens": prompt_tokens + completion_tokens,
            },
        },
    ]
    lines: list[str] = []
    for chunk in chunks:
        lines.append(f"data: {json.dumps(chunk)}")
        lines.append("")
    lines.append("data: [DONE]")
    lines.append("")
    return ("\n".join(lines) + "\n").encode()


def _mock_openai_client(handler: Callable[[httpx2.Request], httpx2.Response]) -> openai.AsyncOpenAI:
    # The `openai` package (like `anthropic`) is built on `httpx2`, not `httpx` -- its
    # `http_client` override must be an `httpx2.AsyncClient`.
    return openai.AsyncOpenAI(
        api_key="test-key",
        http_client=httpx2.AsyncClient(transport=httpx2.MockTransport(handler)),
    )


def _auth_headers(user_id: uuid.UUID, session_id: uuid.UUID) -> dict[str, str]:
    return {
        "Authorization": f"Bearer {settings.internal_proxy_token}",
        "X-Local-Agent-User-Id": str(user_id),
        "X-Local-Agent-Session-Id": str(session_id),
    }


async def test_create_message_without_valid_token_returns_401(client: AsyncClient) -> None:
    response = await client.post(
        "/v1/messages",
        headers=_auth_headers(uuid.uuid4(), uuid.uuid4()) | {"Authorization": "Bearer wrong-token"},
        json={"model": "claude-sonnet-5", "max_tokens": 10, "messages": []},
    )
    assert response.status_code == 401


async def test_create_message_streams_deltas_and_records_usage(
    client: AsyncClient, db_session: AsyncSession, seed_user: uuid.UUID
) -> None:
    session_id = uuid.uuid4()
    db_session.add(ChatSession(id=session_id, user_id=seed_user))
    await db_session.flush()

    def handler(request: httpx.Request) -> httpx.Response:
        assert request.headers["x-api-key"] == settings.anthropic_api_key
        return httpx.Response(200, content=_sse_body("Hello!", 12, 4, "claude-sonnet-5"))

    app.dependency_overrides[get_anthropic_client] = lambda: _mock_anthropic_client(handler)

    response = await client.post(
        "/v1/messages",
        headers=_auth_headers(seed_user, session_id),
        json={
            "model": "claude-sonnet-5",
            "max_tokens": 100,
            "messages": [{"role": "user", "content": [{"type": "text", "text": "hi"}]}],
        },
    )

    assert response.status_code == 200
    assert "Hello!" in response.text

    counter = await db_session.get(UserUsageCounter, (seed_user, _PERIOD_START))
    assert counter is not None
    # user_usage_counters.spent_usd is Numeric(12, 4); Postgres rounds on write, so compare
    # against the same 4-decimal-place rounding rather than the raw unrounded Decimal.
    expected = metering.estimate_cost_usd("claude-sonnet-5", 12, 4).quantize(
        Decimal("0.0001"), rounding=ROUND_HALF_UP
    )
    assert counter.spent_usd == expected


async def test_create_message_over_budget_returns_402_and_never_calls_upstream(
    client: AsyncClient, db_session: AsyncSession, seed_user: uuid.UUID
) -> None:
    db_session.add(UserUsageLimit(user_id=seed_user, monthly_budget_usd=Decimal("0.01")))
    db_session.add(UserUsageCounter(user_id=seed_user, period_start=_PERIOD_START, spent_usd=Decimal("0.01")))
    await db_session.flush()

    called = False

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal called
        called = True
        return httpx.Response(200, content=_sse_body("nope", 1, 1, "claude-sonnet-5"))

    app.dependency_overrides[get_anthropic_client] = lambda: _mock_anthropic_client(handler)

    response = await client.post(
        "/v1/messages",
        headers=_auth_headers(seed_user, uuid.uuid4()),
        json={
            "model": "claude-sonnet-5",
            "max_tokens": 10,
            "messages": [{"role": "user", "content": [{"type": "text", "text": "hi"}]}],
        },
    )

    assert response.status_code == 402
    assert called is False


async def test_create_message_routes_non_claude_model_through_openai_backend(
    client: AsyncClient, db_session: AsyncSession, seed_user: uuid.UUID
) -> None:
    session_id = uuid.uuid4()
    db_session.add(ChatSession(id=session_id, user_id=seed_user))
    await db_session.flush()

    def handler(request: httpx2.Request) -> httpx2.Response:
        return httpx2.Response(
            200,
            content=_openai_sse_body("Hi from GPT!", 9, 3, "gpt-4o-mini"),
            headers={"content-type": "text/event-stream"},
        )

    app.dependency_overrides[get_openai_client] = lambda: _mock_openai_client(handler)

    response = await client.post(
        "/v1/messages",
        headers=_auth_headers(seed_user, session_id),
        json={
            "model": "gpt-4o-mini",
            "max_tokens": 100,
            "messages": [{"role": "user", "content": [{"type": "text", "text": "hi"}]}],
        },
    )

    assert response.status_code == 200
    assert "Hi from GPT!" in response.text

    counter = await db_session.get(UserUsageCounter, (seed_user, _PERIOD_START))
    assert counter is not None
    expected = metering.estimate_cost_usd("gpt-4o-mini", 9, 3).quantize(
        Decimal("0.0001"), rounding=ROUND_HALF_UP
    )
    assert counter.spent_usd == expected


async def test_create_message_routes_local_model_alias_through_openai_backend(
    client: AsyncClient, db_session: AsyncSession, seed_user: uuid.UUID
) -> None:
    session_id = uuid.uuid4()
    db_session.add(ChatSession(id=session_id, user_id=seed_user))
    await db_session.flush()

    def handler(request: httpx2.Request) -> httpx2.Response:
        return httpx2.Response(
            200,
            content=_openai_sse_body("Hi from local model!", 5, 2, settings.local_model_id),
            headers={"content-type": "text/event-stream"},
        )

    app.dependency_overrides[get_local_model_client] = lambda: _mock_openai_client(handler)

    response = await client.post(
        "/v1/messages",
        headers=_auth_headers(seed_user, session_id),
        json={
            "model": "local-model",
            "max_tokens": 100,
            "messages": [{"role": "user", "content": [{"type": "text", "text": "hi"}]}],
        },
    )

    assert response.status_code == 200
    assert "Hi from local model!" in response.text


async def test_create_message_returns_503_when_openai_backend_not_configured(
    client: AsyncClient, seed_user: uuid.UUID
) -> None:
    # Default fixture state: get_openai_client/get_local_model_client resolve to None, matching
    # a deployment with no OPENAI_API_KEY / LOCAL_MODEL_BASE_URL configured.
    response = await client.post(
        "/v1/messages",
        headers=_auth_headers(seed_user, uuid.uuid4()),
        json={
            "model": "gpt-4o-mini",
            "max_tokens": 10,
            "messages": [{"role": "user", "content": [{"type": "text", "text": "hi"}]}],
        },
    )

    assert response.status_code == 503
