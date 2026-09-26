import asyncio
import json
import logging
import uuid
from collections.abc import AsyncIterator, Coroutine
from dataclasses import dataclass, field

import httpx
from fastapi import HTTPException, status

from api.config import settings
from api.db.session import AsyncSessionLocal
from shared.db.models import Message

logger = logging.getLogger(__name__)


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


@dataclass
class _Generation:
    subscribers: list[asyncio.Queue] = field(default_factory=list)
    accumulated_text: str = ""
    task: asyncio.Task | None = None


class SessionTaskManager:
    """In-process registry of running chat generations, one per `session_id`.

    Lives on `app.state` (see `api/main.py`), analogous to `app.state.llm_proxy_client`. Each
    generation is an `asyncio.Task` running `run_generation`, plus the subscriber queues (one per
    connected browser tab) it fans deltas out to, plus the text accumulated so far so a subscriber
    joining mid-generation can be replayed everything it missed.

    Only correct within a single `api` process: the registry is plain process memory, not shared
    across workers. That is a deliberate limit of this stage, not an oversight -- see the Stage 3
    task notes on horizontal scaling.
    """

    def __init__(self) -> None:
        self._generations: dict[uuid.UUID, _Generation] = {}

    def start(
        self, session_id: uuid.UUID, generation: Coroutine[None, None, str | None]
    ) -> asyncio.Queue:
        """Start a new generation for `session_id` and return its first subscriber queue.

        `generation` is an unstarted `run_generation(...)` coroutine; it becomes the `asyncio.Task`
        driving this generation. Raises `409` if one is already in progress for this session.
        """
        if session_id in self._generations:
            generation.close()
            raise HTTPException(
                status.HTTP_409_CONFLICT, "A generation is already in progress for this session"
            )
        queue: asyncio.Queue = asyncio.Queue()
        state = _Generation(subscribers=[queue])
        self._generations[session_id] = state
        state.task = asyncio.create_task(self._drive(session_id, generation))
        return queue

    def subscribe(self, session_id: uuid.UUID) -> asyncio.Queue | None:
        """Attach a new subscriber queue to an in-progress generation, or `None` if none is running.

        The new queue is immediately seeded with a single synthetic `delta` event carrying all text
        accumulated so far, so a late subscriber can render the same content as one that has been
        watching from the start.
        """
        state = self._generations.get(session_id)
        if state is None:
            return None
        queue: asyncio.Queue = asyncio.Queue()
        if state.accumulated_text:
            queue.put_nowait(_sse("delta", {"text": state.accumulated_text}))
        state.subscribers.append(queue)
        return queue

    def cancel(self, session_id: uuid.UUID) -> bool:
        """Cancel the in-progress generation for `session_id`. Returns `False` if none is running."""
        state = self._generations.get(session_id)
        if state is None or state.task is None:
            return False
        state.task.cancel()
        return True

    def publish_delta(self, session_id: uuid.UUID, text: str) -> None:
        """Called by `run_generation` for each chunk of text; fans it out to all current subscribers."""
        state = self._generations.get(session_id)
        if state is None:
            return
        state.accumulated_text += text
        frame = _sse("delta", {"text": text})
        for queue in state.subscribers:
            queue.put_nowait(frame)

    async def _drive(
        self, session_id: uuid.UUID, generation: Coroutine[None, None, str | None]
    ) -> None:
        try:
            error_message = await generation
        except Exception:
            logger.exception("Unhandled error in chat generation for session %s", session_id)
            error_message = "Internal error during generation"
        finally:
            state = self._generations.pop(session_id, None)
            if state is not None:
                frame = _sse("error", {"message": error_message}) if error_message else _sse("done", {})
                for queue in state.subscribers:
                    queue.put_nowait(frame)
                    queue.put_nowait(None)


async def stream_queue(queue: asyncio.Queue) -> AsyncIterator[bytes]:
    """Turn a subscriber queue into the byte stream a `StreamingResponse` reads from.

    `None` is the manager's sentinel for "this subscriber's generation has ended" (pushed right
    after the final `done`/`error` frame), so the generator stops cleanly instead of hanging.
    """
    while True:
        item = await queue.get()
        if item is None:
            break
        yield item


async def run_generation(
    manager: SessionTaskManager,
    *,
    session_id: uuid.UUID,
    user_id: uuid.UUID,
    client: httpx.AsyncClient,
    assistant_sequence_number: int,
    anthropic_messages: list[dict],
) -> str | None:
    """Drive one chat generation to completion, independent of any single HTTP request.

    Runs as the `asyncio.Task` behind a `SessionTaskManager` entry (see `start`): calls llm-proxy,
    translates its Anthropic-shaped SSE into our browser-facing events, publishes each delta to
    every subscriber queue currently registered for `session_id`, and persists the assistant reply
    exactly once when the stream ends -- regardless of how many subscribers are attached, including
    zero. Opens its own DB session via `AsyncSessionLocal` (the same sessionmaker `get_db` uses)
    because it must outlive the HTTP request that started it.

    Cancellation (via `SessionTaskManager.cancel`) is handled the same way as an upstream network
    error: whatever text has accumulated so far is persisted and an `error` event is published,
    rather than introducing a separate "discard partial text" path.

    Returns the error message to report as the generation's terminal event, or `None` on success.
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
                                manager.publish_delta(session_id, text)
                    elif event_type == "error":
                        error_message = data.get("error", {}).get("message", "Upstream error")
    except httpx.HTTPError as exc:
        error_message = f"llm-proxy unreachable: {exc}"
    except asyncio.CancelledError:
        error_message = "Generation cancelled"

    if accumulated_text:
        async with AsyncSessionLocal() as db:
            db.add(
                Message(
                    session_id=session_id,
                    sequence_number=assistant_sequence_number,
                    role="assistant",
                    content=[{"type": "text", "text": accumulated_text}],
                )
            )
            await db.commit()

    return error_message
