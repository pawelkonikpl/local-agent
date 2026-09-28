import json
import uuid
from collections.abc import Callable

import httpx
from httpx import AsyncClient
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from api.auth.security import hash_password
from api.chat.routes import get_llm_proxy_client
from api.config import settings
from api.main import app
from shared.db.models import ChatSession, Message, TokenUsage


def _sse_body(text: str) -> bytes:
    events: list[tuple[str, dict]] = [
        (
            "message_start",
            {"type": "message_start", "message": {"model": "claude-sonnet-5", "usage": {"input_tokens": 5, "output_tokens": 0}}},
        ),
        (
            "content_block_delta",
            {"type": "content_block_delta", "index": 0, "delta": {"type": "text_delta", "text": text}},
        ),
        ("message_delta", {"type": "message_delta", "delta": {"stop_reason": "end_turn"}, "usage": {"output_tokens": 3}}),
        ("message_stop", {"type": "message_stop"}),
    ]
    lines: list[str] = []
    for event_type, data in events:
        lines.append(f"event: {event_type}")
        lines.append(f"data: {json.dumps(data)}")
        lines.append("")
    return ("\n".join(lines) + "\n").encode()


# What llm-proxy's `GET /v1/models` answers in these tests -- in production it asks the providers.
_PROXY_MODELS = [
    {"id": "claude-opus-5-5", "reasoning_efforts": ["low", "medium", "high", "xhigh", "max"]},
    {"id": "claude-sonnet-5", "reasoning_efforts": ["low", "medium", "high", "xhigh", "max"]},
    {"id": "claude-haiku-4-5-20251001", "reasoning_efforts": []},
    {"id": "gpt-6-luna", "reasoning_efforts": ["low", "medium", "high"]},
]


def _mock_llm_proxy_client(
    handler: Callable[[httpx.Request], httpx.Response], models: list[dict] = _PROXY_MODELS
) -> httpx.AsyncClient:
    """`handler` serves `/v1/messages`; `/v1/models` answers with `models`."""

    def route(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/v1/models":
            assert request.headers["authorization"] == f"Bearer {settings.internal_proxy_token}"
            return httpx.Response(200, json={"models": models})
        return handler(request)

    return httpx.AsyncClient(transport=httpx.MockTransport(route), base_url="http://mock-llm-proxy.test")


def _no_generation(request: httpx.Request) -> httpx.Response:
    raise AssertionError("llm-proxy must not be asked to generate in this test")


async def _create_user_and_login(client: AsyncClient, db_session: AsyncSession, email: str) -> None:
    from api.db.models.user import User

    user = User(email=email, password_hash=hash_password("test-pass-123"), role="user")
    db_session.add(user)
    await db_session.flush()
    await client.post("/auth/login", json={"email": email, "password": "test-pass-123"})


async def test_create_session_without_cookie_returns_401(client: AsyncClient) -> None:
    response = await client.post("/sessions")
    assert response.status_code == 401


async def test_create_session_with_cookie_returns_201_with_id(
    client: AsyncClient, db_session: AsyncSession
) -> None:
    await _create_user_and_login(client, db_session, "ivan@example.com")

    response = await client.post("/sessions")

    assert response.status_code == 201
    assert "id" in response.json()


async def test_list_sessions_only_returns_own_sessions(client: AsyncClient, db_session: AsyncSession) -> None:
    from api.db.models.user import User

    owner = User(email="julia@example.com", password_hash=hash_password("test-pass-123"), role="user")
    other = User(email="kim@example.com", password_hash=hash_password("test-pass-123"), role="user")
    db_session.add_all([owner, other])
    await db_session.flush()
    db_session.add(ChatSession(user_id=owner.id))
    db_session.add(ChatSession(user_id=other.id))
    await db_session.flush()

    await client.post("/auth/login", json={"email": "julia@example.com", "password": "test-pass-123"})
    response = await client.get("/sessions")

    assert response.status_code == 200
    body = response.json()
    assert len(body) == 1


async def test_get_messages_for_other_users_session_returns_404(
    client: AsyncClient, db_session: AsyncSession
) -> None:
    from api.db.models.user import User

    owner = User(email="leo@example.com", password_hash=hash_password("test-pass-123"), role="user")
    other = User(email="mia@example.com", password_hash=hash_password("test-pass-123"), role="user")
    db_session.add_all([owner, other])
    await db_session.flush()
    other_session = ChatSession(user_id=other.id)
    db_session.add(other_session)
    await db_session.flush()

    await client.post("/auth/login", json={"email": "leo@example.com", "password": "test-pass-123"})
    response = await client.get(f"/sessions/{other_session.id}/messages")

    assert response.status_code == 404


async def test_post_message_streams_deltas_and_persists_both_messages(
    client: AsyncClient, db_session: AsyncSession
) -> None:
    await _create_user_and_login(client, db_session, "nina@example.com")
    create_response = await client.post("/sessions")
    session_id = uuid.UUID(create_response.json()["id"])

    def handler(request: httpx.Request) -> httpx.Response:
        assert request.headers["x-local-agent-session-id"] == str(session_id)
        return httpx.Response(200, content=_sse_body("Hello there!"))

    app.dependency_overrides[get_llm_proxy_client] = lambda: _mock_llm_proxy_client(handler)

    response = await client.post(f"/sessions/{session_id}/messages", json={"content": "hi"})

    assert response.status_code == 200
    assert "event: delta" in response.text
    assert "event: done" in response.text

    messages_response = await client.get(f"/sessions/{session_id}/messages")
    messages = messages_response.json()
    assert [m["role"] for m in messages] == ["user", "assistant"]
    assert messages[0]["sequence_number"] < messages[1]["sequence_number"]
    assert messages[1]["content"][0]["text"] == "Hello there!"


async def test_post_message_when_llm_proxy_rejects_saves_no_assistant_message(
    client: AsyncClient, db_session: AsyncSession
) -> None:
    await _create_user_and_login(client, db_session, "oscar@example.com")
    create_response = await client.post("/sessions")
    session_id = uuid.UUID(create_response.json()["id"])

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(402, json={"detail": "Monthly budget exceeded."})

    app.dependency_overrides[get_llm_proxy_client] = lambda: _mock_llm_proxy_client(handler)

    response = await client.post(f"/sessions/{session_id}/messages", json={"content": "hi"})

    assert response.status_code == 200
    assert "event: error" in response.text
    assert "budget" in response.text.lower()

    result = await db_session.execute(select(Message).where(Message.session_id == session_id))
    roles = [m.role for m in result.scalars()]
    assert roles == ["user"]


async def test_get_usage_sums_token_usage_rows_for_the_session(
    client: AsyncClient, db_session: AsyncSession
) -> None:
    from api.db.models.user import User

    user = User(email="paula@example.com", password_hash=hash_password("test-pass-123"), role="user")
    db_session.add(user)
    await db_session.flush()
    session = ChatSession(user_id=user.id)
    db_session.add(session)
    await db_session.flush()
    db_session.add_all(
        [
            TokenUsage(session_id=session.id, user_id=user.id, model="claude-sonnet-5", input_tokens=10, output_tokens=5),
            TokenUsage(session_id=session.id, user_id=user.id, model="claude-sonnet-5", input_tokens=20, output_tokens=8),
        ]
    )
    await db_session.flush()

    await client.post("/auth/login", json={"email": "paula@example.com", "password": "test-pass-123"})
    response = await client.get(f"/sessions/{session.id}/usage")

    assert response.status_code == 200
    assert response.json() == {"input_tokens": 30, "output_tokens": 13, "total_tokens": 43}


async def test_get_usage_with_no_recorded_tokens_returns_zeros(
    client: AsyncClient, db_session: AsyncSession
) -> None:
    await _create_user_and_login(client, db_session, "quinn@example.com")
    create_response = await client.post("/sessions")
    session_id = create_response.json()["id"]

    response = await client.get(f"/sessions/{session_id}/usage")

    assert response.status_code == 200
    assert response.json() == {"input_tokens": 0, "output_tokens": 0, "total_tokens": 0}


async def test_get_usage_for_other_users_session_returns_404(
    client: AsyncClient, db_session: AsyncSession
) -> None:
    from api.db.models.user import User

    owner = User(email="rex@example.com", password_hash=hash_password("test-pass-123"), role="user")
    other = User(email="sara@example.com", password_hash=hash_password("test-pass-123"), role="user")
    db_session.add_all([owner, other])
    await db_session.flush()
    other_session = ChatSession(user_id=other.id)
    db_session.add(other_session)
    await db_session.flush()

    await client.post("/auth/login", json={"email": "rex@example.com", "password": "test-pass-123"})
    response = await client.get(f"/sessions/{other_session.id}/usage")

    assert response.status_code == 404


async def test_list_models_returns_default_first(client: AsyncClient, db_session: AsyncSession) -> None:
    await _create_user_and_login(client, db_session, "quinn@example.com")
    app.dependency_overrides[get_llm_proxy_client] = lambda: _mock_llm_proxy_client(_no_generation)

    response = await client.get("/models")

    assert response.status_code == 200
    body = response.json()
    assert body["default"] == settings.chat_model
    assert body["models"][0]["id"] == settings.chat_model
    assert {model["id"] for model in body["models"]} == {model["id"] for model in _PROXY_MODELS}


async def test_list_models_reports_the_providers_reasoning_efforts(client: AsyncClient, db_session: AsyncSession) -> None:
    await _create_user_and_login(client, db_session, "quincy@example.com")
    app.dependency_overrides[get_llm_proxy_client] = lambda: _mock_llm_proxy_client(_no_generation)

    response = await client.get("/models")

    efforts = {model["id"]: model["reasoning_efforts"] for model in response.json()["models"]}
    assert efforts == {model["id"]: model["reasoning_efforts"] for model in _PROXY_MODELS}


async def test_list_models_defaults_to_the_first_listed_when_chat_model_is_not_offered(
    client: AsyncClient, db_session: AsyncSession
) -> None:
    await _create_user_and_login(client, db_session, "quentin@example.com")
    offered = [model for model in _PROXY_MODELS if model["id"] != settings.chat_model]
    app.dependency_overrides[get_llm_proxy_client] = lambda: _mock_llm_proxy_client(_no_generation, offered)

    response = await client.get("/models")

    body = response.json()
    assert body["default"] == offered[0]["id"]
    assert body["models"][0]["id"] == offered[0]["id"]


async def test_list_models_when_llm_proxy_cannot_list_returns_502(client: AsyncClient, db_session: AsyncSession) -> None:
    await _create_user_and_login(client, db_session, "quigley@example.com")

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(502, json={"detail": "No provider could list its models"})

    app.dependency_overrides[get_llm_proxy_client] = lambda: httpx.AsyncClient(
        transport=httpx.MockTransport(handler), base_url="http://mock-llm-proxy.test"
    )

    response = await client.get("/models")

    assert response.status_code == 502


async def test_post_message_forwards_the_chosen_model(client: AsyncClient, db_session: AsyncSession) -> None:
    await _create_user_and_login(client, db_session, "rosa@example.com")
    session_id = (await client.post("/sessions")).json()["id"]
    chosen = _PROXY_MODELS[-1]["id"]
    sent_models: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        sent_models.append(json.loads(request.content)["model"])
        return httpx.Response(200, content=_sse_body("ok"))

    app.dependency_overrides[get_llm_proxy_client] = lambda: _mock_llm_proxy_client(handler)

    response = await client.post(f"/sessions/{session_id}/messages", json={"content": "hi", "model": chosen})

    assert response.status_code == 200
    assert sent_models == [chosen]


async def test_post_message_forwards_reasoning_effort_as_output_config(
    client: AsyncClient, db_session: AsyncSession
) -> None:
    await _create_user_and_login(client, db_session, "rory@example.com")
    session_id = (await client.post("/sessions")).json()["id"]
    sent_payloads: list[dict] = []

    def handler(request: httpx.Request) -> httpx.Response:
        sent_payloads.append(json.loads(request.content))
        return httpx.Response(200, content=_sse_body("ok"))

    app.dependency_overrides[get_llm_proxy_client] = lambda: _mock_llm_proxy_client(handler)

    response = await client.post(
        f"/sessions/{session_id}/messages",
        json={"content": "hi", "model": "claude-sonnet-5", "reasoning_effort": "high"},
    )

    assert response.status_code == 200
    assert [payload["output_config"] for payload in sent_payloads] == [{"effort": "high"}]


async def test_post_message_without_reasoning_effort_sends_no_output_config(
    client: AsyncClient, db_session: AsyncSession
) -> None:
    await _create_user_and_login(client, db_session, "ruth@example.com")
    session_id = (await client.post("/sessions")).json()["id"]
    sent_payloads: list[dict] = []

    def handler(request: httpx.Request) -> httpx.Response:
        sent_payloads.append(json.loads(request.content))
        return httpx.Response(200, content=_sse_body("ok"))

    app.dependency_overrides[get_llm_proxy_client] = lambda: _mock_llm_proxy_client(handler)

    response = await client.post(f"/sessions/{session_id}/messages", json={"content": "hi", "model": "claude-sonnet-5"})

    assert response.status_code == 200
    assert "output_config" not in sent_payloads[0]


async def test_post_message_with_unsupported_reasoning_effort_returns_422_and_saves_nothing(
    client: AsyncClient, db_session: AsyncSession
) -> None:
    await _create_user_and_login(client, db_session, "sid@example.com")
    session_id = uuid.UUID((await client.post("/sessions")).json()["id"])

    app.dependency_overrides[get_llm_proxy_client] = lambda: _mock_llm_proxy_client(_no_generation)

    response = await client.post(
        f"/sessions/{session_id}/messages",
        json={"content": "hi", "model": "claude-haiku-4-5-20251001", "reasoning_effort": "high"},
    )

    assert response.status_code == 422
    result = await db_session.execute(select(Message).where(Message.session_id == session_id))
    assert list(result.scalars()) == []


async def test_post_message_with_unknown_model_returns_422_and_saves_nothing(
    client: AsyncClient, db_session: AsyncSession
) -> None:
    await _create_user_and_login(client, db_session, "sam@example.com")
    session_id = uuid.UUID((await client.post("/sessions")).json()["id"])

    app.dependency_overrides[get_llm_proxy_client] = lambda: _mock_llm_proxy_client(_no_generation)

    response = await client.post(f"/sessions/{session_id}/messages", json={"content": "hi", "model": "gpt-nope"})

    assert response.status_code == 422
    result = await db_session.execute(select(Message).where(Message.session_id == session_id))
    assert list(result.scalars()) == []
