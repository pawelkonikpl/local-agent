"""Chat generation: the model/tool loop and its fan-out to browser tabs.

SSE contract towards the browser (every frame is `event: <name>` + one JSON `data:` line):

- `delta {text}` -- a chunk of assistant text,
- `tool_call {id, name, input}` -- the model called a tool (sent once its input is complete),
- `tool_result {id, content, is_error}` -- that tool's result, matched by `id`,
- `done {}` / `error {message}` -- terminal: the generation ended, successfully or not.
"""

import asyncio
import itertools
import json
import logging
import uuid
from collections.abc import AsyncIterator, Coroutine
from dataclasses import dataclass, field
from datetime import UTC, datetime

import httpx
from fastapi import HTTPException, status
from shared.db.models import Message
from sqlalchemy import null
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from api.chat.anthropic_stream import MALFORMED_INPUT_MESSAGE, TextDelta, ToolUseComplete, TurnAccumulator
from api.chat.prompts import web_rules
from api.config import settings
from api.db.models.tool_call_event import ToolCallEvent
from api.db.session import AsyncSessionLocal
from api.tools.base import ToolResult
from api.tools.guard import GenerationGuard
from api.tools.registry import ToolRegistry

logger = logging.getLogger(__name__)

# The GUI matches this exact wording to show a stop rather than an error banner.
CANCELLED_MESSAGE = "Generation cancelled"
CANCELLED_TOOL_MESSAGE = "Tool execution cancelled"


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
    # SSE frames a late subscriber must be replayed, in order -- except the text published since
    # the last non-`delta` frame, kept as chunks in `trailing_text` and flushed into `replay` as a
    # single `delta` frame. Replay therefore grows per tool call, not per token.
    replay: list[bytes] = field(default_factory=list)
    trailing_text: list[str] = field(default_factory=list)
    task: asyncio.Task | None = None

    def flush_trailing_text(self) -> None:
        if self.trailing_text:
            self.replay.append(_sse("delta", {"text": "".join(self.trailing_text)}))
            self.trailing_text.clear()


class SessionTaskManager:
    """In-process registry of running chat generations, one per `session_id`.

    Lives on `app.state` (see `api/main.py`), analogous to `app.state.llm_proxy_client`. Each
    generation is an `asyncio.Task` running `run_generation`, plus the subscriber queues (one per
    connected browser tab) it fans events out to, plus a replay of everything published so far so
    a subscriber joining mid-generation can be sent what it missed.

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

        The new queue is immediately seeded with the replay: every `tool_call`/`tool_result` so
        far, with the text between them merged into one `delta` per stretch, so a late subscriber
        can render the same content as one that has been watching from the start.
        """
        state = self._generations.get(session_id)
        if state is None:
            return None
        state.flush_trailing_text()
        queue: asyncio.Queue = asyncio.Queue()
        for frame in state.replay:
            queue.put_nowait(frame)
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
        state.trailing_text.append(text)
        self._fan_out(state, _sse("delta", {"text": text}))

    def publish_event(self, session_id: uuid.UUID, event: str, data: dict) -> None:
        """Called by `run_generation` for non-text events (`tool_call`, `tool_result`)."""
        state = self._generations.get(session_id)
        if state is None:
            return
        frame = _sse(event, data)
        state.flush_trailing_text()
        state.replay.append(frame)
        self._fan_out(state, frame)

    @staticmethod
    def _fan_out(state: _Generation, frame: bytes) -> None:
        for queue in state.subscribers:
            queue.put_nowait(frame)

    async def _drive(
        self, session_id: uuid.UUID, generation: Coroutine[None, None, str | None]
    ) -> None:
        try:
            error_message = await generation
        except asyncio.CancelledError:
            # `run_generation` handles cancellation itself; this only catches one landing outside
            # its guarded awaits (e.g. mid DB write), so subscribers still get a terminal event.
            error_message = CANCELLED_MESSAGE
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
    tools: ToolRegistry,
    model: str,
    reasoning_effort: str | None,
    first_sequence_number: int,
    anthropic_messages: list[dict],
) -> str | None:
    """Drive one chat generation -- possibly several model turns -- independent of any request.

    Runs as the `asyncio.Task` behind a `SessionTaskManager` entry (see `start`). Each iteration
    streams one model turn from llm-proxy, publishing text and completed tool calls to every
    subscriber of `session_id`, and persists the assistant message. If the turn ended in
    `tool_use`, the tools run (sequentially, in auto mode -- `approval_mode` isn't honoured yet),
    their results are persisted as a `user` message and the model is called again, up to
    `settings.chat_max_tool_iterations` model calls. Messages get consecutive sequence numbers
    starting at `first_sequence_number`. Opens its own DB sessions via `AsyncSessionLocal` (the
    same sessionmaker `get_db` uses) because it must outlive the HTTP request that started it.

    Invariant: every persisted `tool_use` has its `tool_result` in the next persisted message. So
    a turn interrupted mid-stream (cancel, network error, upstream `error`) persists only its
    text, and a cancel during tool execution answers the pending calls with a synthetic error
    result before stopping.

    Returns the error message to report as the generation's terminal event, or `None` on success.
    """
    headers = {
        "Authorization": f"Bearer {settings.internal_proxy_token}",
        "X-Local-Agent-User-Id": str(user_id),
        "X-Local-Agent-Session-Id": str(session_id),
    }
    tool_definitions = tools.definitions() if settings.chat_tools_enabled else []
    guard = GenerationGuard()
    # Only in the payload, never persisted: the canary in it must not outlive this generation.
    system_prompt = web_rules(guard.canary) if tool_definitions and tools.reads_untrusted() else None
    history = list(anthropic_messages)
    sequence_numbers = itertools.count(first_sequence_number)

    for _ in range(settings.chat_max_tool_iterations):
        payload = {
            "model": model,
            "max_tokens": settings.chat_max_tokens,
            "messages": history,
            "stream": True,
        }
        if reasoning_effort is not None:
            payload["output_config"] = {"effort": reasoning_effort}
        if tool_definitions:
            payload["tools"] = tool_definitions
        if system_prompt is not None:
            payload["system"] = system_prompt

        turn = TurnAccumulator()
        try:
            error_message = await _stream_turn(manager, session_id, client, payload, headers, turn)
        except asyncio.CancelledError:
            error_message = CANCELLED_MESSAGE

        guard.check_output("".join(block.get("text", "") for block in turn.text_blocks))
        tool_uses = turn.tool_uses
        if error_message is not None or turn.stop_reason != "tool_use" or not tool_uses:
            # Nothing will answer this turn's tool calls, so only its text is kept.
            if turn.text_blocks:
                await _save_message(session_id, next(sequence_numbers), "assistant", turn.text_blocks)
            return error_message

        assistant_message = await _save_message(
            session_id, next(sequence_numbers), "assistant", turn.content_blocks
        )
        results, was_cancelled = await _run_tools(
            manager, session_id, tools, tool_uses, message_id=assistant_message.id, guard=guard
        )
        await _save_message(session_id, next(sequence_numbers), "user", results)
        if was_cancelled:
            return CANCELLED_MESSAGE
        history += [
            {"role": "assistant", "content": turn.content_blocks},
            {"role": "user", "content": results},
        ]

    return f"Tool iteration limit reached ({settings.chat_max_tool_iterations})"


async def _stream_turn(
    manager: SessionTaskManager,
    session_id: uuid.UUID,
    client: httpx.AsyncClient,
    payload: dict,
    headers: dict,
    turn: TurnAccumulator,
) -> str | None:
    """Stream one model turn into `turn`, publishing as it goes. Returns an error message, if any."""
    try:
        async with client.stream("POST", "/v1/messages", json=payload, headers=headers) as response:
            if response.status_code >= 400:
                body = await response.aread()
                return _extract_error_detail(body, response.status_code)
            async for event_type, data in _iter_sse(response):
                for event in turn.feed(event_type, data):
                    match event:
                        case TextDelta(text=text):
                            manager.publish_delta(session_id, text)
                        case ToolUseComplete(id=tool_id, name=name, input=tool_input):
                            manager.publish_event(
                                session_id, "tool_call", {"id": tool_id, "name": name, "input": tool_input}
                            )
    except httpx.HTTPError as exc:
        return f"llm-proxy unreachable: {exc}"
    return turn.error_message


async def _iter_sse(response: httpx.Response) -> AsyncIterator[tuple[str, dict]]:
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
        if isinstance(data, dict):
            yield event_type, data


async def _run_tools(
    manager: SessionTaskManager,
    session_id: uuid.UUID,
    tools: ToolRegistry,
    tool_uses: list[ToolUseComplete],
    *,
    message_id: uuid.UUID,
    guard: GenerationGuard,
) -> tuple[list[dict], bool]:
    """Execute a turn's tool calls in order, recording each in `tool_call_events`.

    Returns the `tool_result` blocks (one per call, in call order) and whether the generation was
    cancelled meanwhile -- in which case the unfinished calls got a synthetic error result.
    """
    event_ids = {tool_use.id: uuid.uuid4() for tool_use in tool_uses}
    results: list[dict] = []

    async def finish(tool_use: ToolUseComplete, result: ToolResult) -> None:
        async with AsyncSessionLocal() as db:
            event_status = "failed" if result.is_error else "succeeded"
            await _write_tool_event(
                db, event_ids[tool_use.id], session_id, message_id, tool_use, status=event_status, result=result
            )
            await db.commit()
        manager.publish_event(
            session_id, "tool_result", {"id": tool_use.id, "content": result.content, "is_error": result.is_error}
        )
        results.append(_tool_result_block(tool_use.id, result))

    try:
        for tool_use in tool_uses:
            async with AsyncSessionLocal() as db:
                await _write_tool_event(db, event_ids[tool_use.id], session_id, message_id, tool_use, status="running")
                await db.commit()
            if tool_use.input_error:
                result = ToolResult(MALFORMED_INPUT_MESSAGE, is_error=True)
            else:
                result = await tools.execute(tool_use.name, tool_use.input, guard=guard)
            await finish(tool_use, result)
    except asyncio.CancelledError:
        # Not re-awaiting the cancelled tool: the pending calls are answered from here instead.
        cancelled = ToolResult(CANCELLED_TOOL_MESSAGE, is_error=True)
        for tool_use in tool_uses[len(results) :]:
            await finish(tool_use, cancelled)
        return results, True
    return results, False


async def _write_tool_event(
    db: AsyncSession,
    event_id: uuid.UUID,
    session_id: uuid.UUID,
    message_id: uuid.UUID,
    tool_use: ToolUseComplete,
    *,
    status: str,
    result: ToolResult | None = None,
) -> None:
    """Insert or update a `tool_call_events` row.

    An upsert keyed by a pre-assigned id, so finishing a call works whether or not its `running`
    row made it to the DB (a cancel can land in between).
    """
    # `null()` rather than `None`: a Core insert would store `None` as JSON `null` in JSONB.
    output = {"content": result.content, "is_error": result.is_error} if result is not None else null()
    completed_at = datetime.now(UTC) if result is not None else None
    statement = pg_insert(ToolCallEvent).values(
        id=event_id,
        session_id=session_id,
        message_id=message_id,
        tool_name=tool_use.name,
        input=tool_use.input,
        status=status,
        output=output,
        completed_at=completed_at,
    )
    await db.execute(
        statement.on_conflict_do_update(
            index_elements=[ToolCallEvent.id],
            set_={"status": status, "output": output, "completed_at": completed_at},
        )
    )


def _tool_result_block(tool_use_id: str, result: ToolResult) -> dict:
    return {"type": "tool_result", "tool_use_id": tool_use_id, "content": result.content, "is_error": result.is_error}


async def _save_message(session_id: uuid.UUID, sequence_number: int, role: str, content: list[dict]) -> Message:
    async with AsyncSessionLocal() as db:
        message = Message(session_id=session_id, sequence_number=sequence_number, role=role, content=content)
        db.add(message)
        await db.commit()
    return message
