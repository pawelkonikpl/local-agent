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


def _responses_event(event_type: str, sequence_number: int, **fields: object) -> dict:
    return {"type": event_type, "sequence_number": sequence_number, **fields}


def _response_object(model: str, status: str, output: list[dict], usage: dict | None, **fields: object) -> dict:
    return {
        "id": "resp_1",
        "object": "response",
        "created_at": 1,
        "model": model,
        "status": status,
        "output": output,
        "usage": usage,
        "error": None,
        "incomplete_details": None,
        "parallel_tool_calls": True,
        "tool_choice": "auto",
        "tools": [],
        **fields,
    }


def _responses_usage(input_tokens: int, output_tokens: int, reasoning_tokens: int = 0) -> dict:
    return {
        "input_tokens": input_tokens,
        "input_tokens_details": {"cached_tokens": 0, "cache_write_tokens": 0},
        "output_tokens": output_tokens,
        "output_tokens_details": {"reasoning_tokens": reasoning_tokens},
        "total_tokens": input_tokens + output_tokens,
    }


def _responses_sse_body(events: list[dict]) -> bytes:
    return "".join(f"event: {event['type']}\ndata: {json.dumps(event)}\n\n" for event in events).encode()


def _responses_text_body(text: str, input_tokens: int, output_tokens: int, model: str) -> bytes:
    message = {"type": "message", "id": "msg_1", "role": "assistant", "status": "in_progress", "content": []}
    return _responses_sse_body(
        [
            _responses_event("response.created", 0, response=_response_object(model, "in_progress", [], None)),
            _responses_event(
                "response.output_item.added",
                1,
                output_index=0,
                item={"type": "reasoning", "id": "rs_1", "summary": []},
            ),
            _responses_event("response.output_item.added", 2, output_index=1, item=message),
            _responses_event(
                "response.output_text.delta",
                3,
                item_id="msg_1",
                output_index=1,
                content_index=0,
                delta=text,
                logprobs=[],
            ),
            _responses_event(
                "response.completed",
                4,
                response=_response_object(
                    model, "completed", [], _responses_usage(input_tokens, output_tokens, reasoning_tokens=2)
                ),
            ),
        ]
    )


def _mock_openai_client(handler: Callable[[httpx2.Request], httpx2.Response]) -> openai.AsyncOpenAI:
    # The `openai` package (like `anthropic`) is built on `httpx2`, not `httpx` -- its
    # `http_client` override must be an `httpx2.AsyncClient`.
    return openai.AsyncOpenAI(
        api_key="test-key",
        http_client=httpx2.AsyncClient(transport=httpx2.MockTransport(handler)),
    )


def _parse_sse(body: str) -> list[tuple[str, dict]]:
    events: list[tuple[str, dict]] = []
    for raw in body.strip().split("\n\n"):
        lines = dict(line.split(": ", 1) for line in raw.splitlines())
        events.append((lines["event"], json.loads(lines["data"])))
    return events


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


async def test_create_message_routes_non_claude_model_through_openai_responses_backend(
    client: AsyncClient, db_session: AsyncSession, seed_user: uuid.UUID
) -> None:
    session_id = uuid.uuid4()
    db_session.add(ChatSession(id=session_id, user_id=seed_user))
    await db_session.flush()

    sent: dict = {}
    sent_path: list[str] = []

    def handler(request: httpx2.Request) -> httpx2.Response:
        sent_path.append(request.url.path)
        sent.update(json.loads(request.content))
        return httpx2.Response(
            200,
            content=_responses_text_body("Hi from GPT!", 9, 3, "gpt-6-sol"),
            headers={"content-type": "text/event-stream"},
        )

    app.dependency_overrides[get_openai_client] = lambda: _mock_openai_client(handler)

    response = await client.post(
        "/v1/messages",
        headers=_auth_headers(seed_user, session_id),
        json={
            "model": "gpt-6-sol",
            "max_tokens": 100,
            "system": "Be brief.",
            "messages": [{"role": "user", "content": [{"type": "text", "text": "hi"}]}],
        },
    )

    assert response.status_code == 200
    assert "Hi from GPT!" in response.text
    assert "end_turn" in response.text
    assert sent_path == ["/v1/responses"]
    assert sent["max_output_tokens"] == 100
    assert sent["instructions"] == "Be brief."
    assert sent["input"] == [{"role": "user", "content": "hi"}]
    assert sent["store"] is False

    counter = await db_session.get(UserUsageCounter, (seed_user, _PERIOD_START))
    assert counter is not None
    expected = metering.estimate_cost_usd("gpt-6-sol", 9, 3).quantize(
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


_WEATHER_TOOL = {
    "name": "get_weather",
    "description": "Current weather for a city",
    "input_schema": {"type": "object", "properties": {"city": {"type": "string"}}},
}


async def test_create_message_streams_openai_function_call_as_tool_use(
    client: AsyncClient, db_session: AsyncSession, seed_user: uuid.UUID
) -> None:
    session_id = uuid.uuid4()
    db_session.add(ChatSession(id=session_id, user_id=seed_user))
    await db_session.flush()

    sent: dict = {}
    call = {"type": "function_call", "id": "fc_1", "call_id": "call_abc", "name": "get_weather", "arguments": ""}
    body = _responses_sse_body(
        [
            _responses_event("response.created", 0, response=_response_object("gpt-6-sol", "in_progress", [], None)),
            _responses_event("response.output_item.added", 1, output_index=0, item=call),
            _responses_event(
                "response.function_call_arguments.delta", 2, item_id="fc_1", output_index=0, delta='{"city": '
            ),
            _responses_event(
                "response.function_call_arguments.delta", 3, item_id="fc_1", output_index=0, delta='"Paris"}'
            ),
            _responses_event(
                "response.completed",
                4,
                response=_response_object("gpt-6-sol", "completed", [], _responses_usage(20, 8, reasoning_tokens=5)),
            ),
        ]
    )

    def handler(request: httpx2.Request) -> httpx2.Response:
        sent.update(json.loads(request.content))
        return httpx2.Response(200, content=body, headers={"content-type": "text/event-stream"})

    app.dependency_overrides[get_openai_client] = lambda: _mock_openai_client(handler)

    response = await client.post(
        "/v1/messages",
        headers=_auth_headers(seed_user, session_id),
        json={
            "model": "gpt-6-sol",
            "max_tokens": 100,
            "tools": [_WEATHER_TOOL],
            "tool_choice": {"type": "tool", "name": "get_weather"},
            "messages": [
                {"role": "user", "content": "weather?"},
                {
                    "role": "assistant",
                    "content": [
                        {"type": "text", "text": "Checking."},
                        {"type": "tool_use", "id": "call_old", "name": "get_weather", "input": {"city": "Rome"}},
                    ],
                },
                {
                    "role": "user",
                    "content": [
                        {"type": "tool_result", "tool_use_id": "call_old", "content": "timeout", "is_error": True},
                        {"type": "text", "text": "try Paris"},
                    ],
                },
            ],
        },
    )

    assert response.status_code == 200
    # Reasoning stays on: nothing switches it off for tool-bearing requests.
    assert "reasoning" not in sent
    assert sent["tools"] == [
        {
            "type": "function",
            "name": "get_weather",
            "parameters": _WEATHER_TOOL["input_schema"],
            "strict": False,
            "description": "Current weather for a city",
        }
    ]
    assert sent["tool_choice"] == {"type": "function", "name": "get_weather"}
    assert sent["input"] == [
        {"role": "user", "content": "weather?"},
        {"role": "assistant", "content": "Checking."},
        {"type": "function_call", "call_id": "call_old", "name": "get_weather", "arguments": '{"city": "Rome"}'},
        {"type": "function_call_output", "call_id": "call_old", "output": "Error: timeout"},
        {"role": "user", "content": "try Paris"},
    ]

    events = _parse_sse(response.text)
    block_start = next(data for event, data in events if event == "content_block_start")
    assert block_start["content_block"] == {"type": "tool_use", "id": "call_abc", "name": "get_weather", "input": {}}
    partial_json = "".join(
        data["delta"]["partial_json"] for event, data in events if event == "content_block_delta"
    )
    assert json.loads(partial_json) == {"city": "Paris"}
    message_delta = next(data for event, data in events if event == "message_delta")
    assert message_delta["delta"]["stop_reason"] == "tool_use"
    # Hidden reasoning tokens are billed as output and included in `output_tokens`.
    assert message_delta["usage"]["output_tokens"] == 8


async def test_create_message_maps_openai_incomplete_response_to_max_tokens(
    client: AsyncClient, db_session: AsyncSession, seed_user: uuid.UUID
) -> None:
    session_id = uuid.uuid4()
    db_session.add(ChatSession(id=session_id, user_id=seed_user))
    await db_session.flush()

    incomplete = _response_object(
        "gpt-6-sol",
        "incomplete",
        [],
        _responses_usage(10, 100, reasoning_tokens=100),
        incomplete_details={"reason": "max_output_tokens"},
    )
    body = _responses_sse_body(
        [
            _responses_event("response.created", 0, response=_response_object("gpt-6-sol", "in_progress", [], None)),
            _responses_event("response.incomplete", 1, response=incomplete),
        ]
    )
    app.dependency_overrides[get_openai_client] = lambda: _mock_openai_client(
        lambda request: httpx2.Response(200, content=body, headers={"content-type": "text/event-stream"})
    )

    response = await client.post(
        "/v1/messages",
        headers=_auth_headers(seed_user, session_id),
        json={"model": "gpt-6-sol", "max_tokens": 100, "messages": [{"role": "user", "content": "hi"}]},
    )

    assert response.status_code == 200
    events = _parse_sse(response.text)
    message_delta = next(data for event, data in events if event == "message_delta")
    assert message_delta["delta"]["stop_reason"] == "max_tokens"
    # Still one (empty) content block, as the `anthropic` SDK expects.
    assert any(event == "content_block_start" for event, _ in events)


async def test_create_message_forwards_openai_failure_as_error_event(
    client: AsyncClient, db_session: AsyncSession, seed_user: uuid.UUID
) -> None:
    session_id = uuid.uuid4()
    db_session.add(ChatSession(id=session_id, user_id=seed_user))
    await db_session.flush()

    failed = _response_object(
        "gpt-6-sol", "failed", [], None, error={"code": "server_error", "message": "The model crashed"}
    )
    body = _responses_sse_body(
        [
            _responses_event("response.created", 0, response=_response_object("gpt-6-sol", "in_progress", [], None)),
            _responses_event("response.failed", 1, response=failed),
        ]
    )
    app.dependency_overrides[get_openai_client] = lambda: _mock_openai_client(
        lambda request: httpx2.Response(200, content=body, headers={"content-type": "text/event-stream"})
    )

    response = await client.post(
        "/v1/messages",
        headers=_auth_headers(seed_user, session_id),
        json={"model": "gpt-6-sol", "max_tokens": 100, "messages": [{"role": "user", "content": "hi"}]},
    )

    assert response.status_code == 200
    error = next(data for event, data in _parse_sse(response.text) if event == "error")
    assert error["error"]["message"] == "The model crashed"


async def test_create_message_with_tools_keeps_local_model_request_untouched(
    client: AsyncClient, db_session: AsyncSession, seed_user: uuid.UUID
) -> None:
    session_id = uuid.uuid4()
    db_session.add(ChatSession(id=session_id, user_id=seed_user))
    await db_session.flush()

    sent: dict = {}

    def handler(request: httpx2.Request) -> httpx2.Response:
        sent.update(json.loads(request.content))
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
            "tools": [_WEATHER_TOOL],
            "messages": [{"role": "user", "content": [{"type": "text", "text": "hi"}]}],
        },
    )

    assert response.status_code == 200
    assert "reasoning_effort" not in sent


async def test_create_message_maps_effort_to_openai_reasoning(
    client: AsyncClient, db_session: AsyncSession, seed_user: uuid.UUID
) -> None:
    session_id = uuid.uuid4()
    db_session.add(ChatSession(id=session_id, user_id=seed_user))
    await db_session.flush()

    sent: dict = {}

    def handler(request: httpx2.Request) -> httpx2.Response:
        sent.update(json.loads(request.content))
        return httpx2.Response(
            200,
            content=_responses_text_body("Hi from GPT!", 9, 3, "gpt-6-sol"),
            headers={"content-type": "text/event-stream"},
        )

    app.dependency_overrides[get_openai_client] = lambda: _mock_openai_client(handler)

    response = await client.post(
        "/v1/messages",
        headers=_auth_headers(seed_user, session_id),
        json={
            "model": "gpt-6-sol",
            "max_tokens": 100,
            "output_config": {"effort": "high"},
            "messages": [{"role": "user", "content": [{"type": "text", "text": "hi"}]}],
        },
    )

    assert response.status_code == 200
    assert sent["reasoning"] == {"effort": "high"}
    assert "output_config" not in sent


async def test_create_message_maps_effort_to_chat_completions_reasoning_effort(
    client: AsyncClient, db_session: AsyncSession, seed_user: uuid.UUID
) -> None:
    session_id = uuid.uuid4()
    db_session.add(ChatSession(id=session_id, user_id=seed_user))
    await db_session.flush()

    sent: dict = {}

    def handler(request: httpx2.Request) -> httpx2.Response:
        sent.update(json.loads(request.content))
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
            "output_config": {"effort": "low"},
            "messages": [{"role": "user", "content": [{"type": "text", "text": "hi"}]}],
        },
    )

    assert response.status_code == 200
    assert sent["reasoning_effort"] == "low"


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
