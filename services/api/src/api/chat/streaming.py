import json
import uuid
from collections.abc import AsyncIterator

import httpx
from sqlalchemy.ext.asyncio import AsyncSession

from api.config import settings
from shared.db.models import Message


def _sse(event: str, data: dict) -> bytes:
    return f"event: {event}\ndata: {json.dumps(data)}\n\n".encode()


def _extract_error_detail(body: bytes, status_code: int) -> str:
    try:
        parsed = json.loads(body)
    except json.JSONDecodeError:
        return f"llm-proxy returned {status_code}"
    if isinstance(parsed, dict):
        detail = parsed.get("detail")
        if detail:
            return str(detail)
    return f"llm-proxy returned {status_code}"


async def stream_chat_response(
    client: httpx.AsyncClient,
    db: AsyncSession,
    *,
    session_id: uuid.UUID,
    user_id: uuid.UUID,
    assistant_sequence_number: int,
    anthropic_messages: list[dict],
) -> AsyncIterator[bytes]:
    """Call llm-proxy, translate its Anthropic-shaped SSE into our browser-facing events, and
    persist the assistant reply once the stream ends.

    Browser-facing event names (`delta`, `error`, `done`) are our own, kept deliberately small so
    a later stage can add `tool_call_proposed`/`tool_call_result` without changing this shape.
    """
    payload = {
        "model": settings.chat_model,
        "max_tokens": settings.chat_max_tokens,
        "messages": anthropic_messages,
        "stream": True,
    }
    headers = {
        "Authorization": f"Bearer {settings.internal_proxy_token}",
        "X-Local-Agent-User-Id": str(user_id),
        "X-Local-Agent-Session-Id": str(session_id),
    }

    accumulated_text = ""
    error_message: str | None = None

    try:
        async with client.stream("POST", "/v1/messages", json=payload, headers=headers) as response:
            if response.status_code >= 400:
                body = await response.aread()
                error_message = _extract_error_detail(body, response.status_code)
            else:
                event_type: str | None = None
                async for line in response.aiter_lines():
                    if line.startswith("event:"):
                        event_type = line[len("event:") :].strip()
                        continue
                    if event_type is None or not line.startswith("data:"):
                        continue
                    raw_data = line[len("data:") :].strip()
                    if not raw_data:
                        continue
                    try:
                        data = json.loads(raw_data)
                    except json.JSONDecodeError:
                        continue

                    if event_type == "content_block_delta":
                        delta = data.get("delta", {})
                        if delta.get("type") == "text_delta":
                            text = delta.get("text", "")
                            if text:
                                accumulated_text += text
                                yield _sse("delta", {"text": text})
                    elif event_type == "error":
                        error_message = data.get("error", {}).get("message", "Upstream error")
    except httpx.HTTPError as exc:
        error_message = f"llm-proxy unreachable: {exc}"

    if accumulated_text:
        db.add(
            Message(
                session_id=session_id,
                sequence_number=assistant_sequence_number,
                role="assistant",
                content=[{"type": "text", "text": accumulated_text}],
            )
        )
        await db.commit()

    if error_message is not None:
        yield _sse("error", {"message": error_message})
    else:
        yield _sse("done", {})
