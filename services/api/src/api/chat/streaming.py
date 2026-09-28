"""Chat generation: the model/tool loop and its fan-out to browser tabs.

SSE contract towards the browser (every frame is `event: <name>` + one JSON `data:` line):

- `delta {text}` -- a chunk of assistant text,
- `tool_call {id, name, input}` -- the model called a tool (sent once its input is complete),
- `tool_result {id, content, is_error}` -- that tool's result, matched by `id`,
- `done {}` / `error {message}` -- terminal: the generation ended, successfully or not.
"""

import asyncio
import itertools
import logging
import uuid
from collections.abc import AsyncIterator, Coroutine
from dataclasses import dataclass, field

from contracts.sse import encode_sse
from fastapi import HTTPException, status

from api.chat.anthropic_stream import (
    MALFORMED_INPUT_MESSAGE,
    TextDelta,
    ToolUseComplete,
    TurnAccumulator,
)
from api.chat.llm_proxy import LlmProxy, LlmProxyError
from api.chat.prompts import web_rules
from api.chat.store import GenerationStore
from api.config import settings
from api.tools.base import ToolResult
from api.tools.guard import GenerationGuard
from api.tools.registry import ToolRegistry

logger = logging.getLogger(__name__)

# The GUI matches this exact wording to show a stop rather than an error banner.
CANCELLED_MESSAGE = "Generation cancelled"
CANCELLED_TOOL_MESSAGE = "Tool execution cancelled"


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
            self.replay.append(encode_sse("delta", {"text": "".join(self.trailing_text)}))
            self.trailing_text.clear()


class SessionTaskManager:
    """In-process registry of running chat generations, one per `session_id`.

    Lives on `app.state` (see `api/main.py`), analogous to `app.state.llm_proxy_client`. Each
    generation is an `asyncio.Task` running `ChatGeneration.run`, plus the subscriber queues (one per
    connected browser tab) it fans events out to, plus a replay of everything published so far so
    a subscriber joining mid-generation can be sent what it missed.

    Only correct within a single `api` process: the registry is plain process memory, not shared
    across workers. That is a deliberate limit of this stage, not an oversight -- see the Stage 3
    task notes on horizontal scaling.
    """

    def __init__(self) -> None:
        self._generations: dict[uuid.UUID, _Generation] = {}

    def reserve(self, session_id: uuid.UUID) -> asyncio.Queue:
        """Claim `session_id` for a new generation and return its first subscriber queue.

        Raises `409` if one is already in progress. A reservation is followed by `start` once the
        generation's input is saved, or by `release` if saving it failed.
        """
        if session_id in self._generations:
            raise HTTPException(
                status.HTTP_409_CONFLICT, "A generation is already in progress for this session"
            )
        queue: asyncio.Queue = asyncio.Queue()
        self._generations[session_id] = _Generation(subscribers=[queue])
        return queue

    def start(self, session_id: uuid.UUID, generation: Coroutine[None, None, str | None]) -> None:
        """Run `generation`, an unstarted `ChatGeneration.run(...)` coroutine, as the `asyncio.Task`
        behind `session_id`'s reservation."""
        self._generations[session_id].task = asyncio.create_task(self._drive(session_id, generation))

    def release(self, session_id: uuid.UUID) -> None:
        """Drop a reservation that never started, ending its subscribers' streams."""
        state = self._generations.get(session_id)
        if state is not None and state.task is None:
            self._finish(session_id, "Generation could not be started")

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
        """Called by `ChatGeneration` for each chunk of text; fans it out to all current subscribers."""
        state = self._generations.get(session_id)
        if state is None:
            return
        state.trailing_text.append(text)
        self._fan_out(state, encode_sse("delta", {"text": text}))

    def publish_event(self, session_id: uuid.UUID, event: str, data: dict) -> None:
        """Called by `ChatGeneration` for non-text events (`tool_call`, `tool_result`)."""
        state = self._generations.get(session_id)
        if state is None:
            return
        frame = encode_sse(event, data)
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
            # `ChatGeneration` handles cancellation itself; this only catches one landing outside
            # its guarded awaits (e.g. mid DB write), so subscribers still get a terminal event.
            error_message = CANCELLED_MESSAGE
        except Exception:
            logger.exception("Unhandled error in chat generation for session %s", session_id)
            error_message = "Internal error during generation"
        finally:
            self._finish(session_id, error_message)

    def _finish(self, session_id: uuid.UUID, error_message: str | None) -> None:
        """Send every subscriber the terminal frame and the end-of-stream sentinel."""
        state = self._generations.pop(session_id, None)
        if state is None:
            return
        frame = encode_sse("error", {"message": error_message}) if error_message else encode_sse("done", {})
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


@dataclass
class ChatGeneration:
    """One chat generation -- possibly several model turns -- independent of any request.

    `run` is the `asyncio.Task` behind a `SessionTaskManager` entry (see `start`). Each iteration
    streams one model turn from llm-proxy, publishing text and completed tool calls to every
    subscriber of `session_id`, and persists the assistant message. If the turn ended in
    `tool_use`, the tools run (sequentially, in auto mode -- `approval_mode` isn't honoured yet),
    their results are persisted as a `user` message and the model is called again, up to
    `settings.chat_max_tool_iterations` model calls.

    Invariant: every persisted `tool_use` has its `tool_result` in the next persisted message. So
    a turn interrupted mid-stream (cancel, network error, upstream `error`) persists only its
    text, and a cancel during tool execution answers the pending calls with a synthetic error
    result before stopping.
    """

    session_id: uuid.UUID
    user_id: uuid.UUID
    model: str
    reasoning_effort: str | None
    manager: SessionTaskManager
    llm: LlmProxy
    store: GenerationStore
    tools: ToolRegistry

    async def run(self, messages: list[dict], *, first_sequence_number: int) -> str | None:
        """Drive the generation on from `messages`; persisted messages get consecutive sequence
        numbers starting at `first_sequence_number`.

        Returns the error message to report as the generation's terminal event, or `None` on success.
        """
        tool_definitions = self.tools.definitions() if settings.chat_tools_enabled else []
        guard = GenerationGuard()
        # Only in the payload, never persisted: the canary in it must not outlive this generation.
        system_prompt = web_rules(guard.canary) if tool_definitions and self.tools.reads_untrusted() else None
        history = list(messages)
        sequence_numbers = itertools.count(first_sequence_number)

        for _ in range(settings.chat_max_tool_iterations):
            payload = {
                "model": self.model,
                "max_tokens": settings.chat_max_tokens,
                "messages": history,
                "stream": True,
            }
            if self.reasoning_effort is not None:
                payload["output_config"] = {"effort": self.reasoning_effort}
            if tool_definitions:
                payload["tools"] = tool_definitions
            if system_prompt is not None:
                payload["system"] = system_prompt

            turn = TurnAccumulator()
            try:
                error_message = await self._stream_turn(payload, turn)
            except asyncio.CancelledError:
                error_message = CANCELLED_MESSAGE

            guard.check_output("".join(block.get("text", "") for block in turn.text_blocks))
            tool_uses = turn.tool_uses
            if error_message is not None or turn.stop_reason != "tool_use" or not tool_uses:
                # Nothing will answer this turn's tool calls, so only its text is kept.
                if turn.text_blocks:
                    await self.store.save_message(self.session_id, next(sequence_numbers), "assistant", turn.text_blocks)
                return error_message

            assistant_message = await self.store.save_message(
                self.session_id, next(sequence_numbers), "assistant", turn.content_blocks
            )
            results, was_cancelled = await self._run_tools(tool_uses, message_id=assistant_message.id, guard=guard)
            await self.store.save_message(self.session_id, next(sequence_numbers), "user", results)
            if was_cancelled:
                return CANCELLED_MESSAGE
            history += [
                {"role": "assistant", "content": turn.content_blocks},
                {"role": "user", "content": results},
            ]

        return f"Tool iteration limit reached ({settings.chat_max_tool_iterations})"

    async def _stream_turn(self, payload: dict, turn: TurnAccumulator) -> str | None:
        """Stream one model turn into `turn`, publishing as it goes. Returns an error message, if any."""
        try:
            async for event_type, data in self.llm.stream_turn(
                payload, user_id=self.user_id, session_id=self.session_id
            ):
                for event in turn.feed(event_type, data):
                    match event:
                        case TextDelta(text=text):
                            self.manager.publish_delta(self.session_id, text)
                        case ToolUseComplete(id=tool_id, name=name, input=tool_input):
                            self.manager.publish_event(
                                self.session_id, "tool_call", {"id": tool_id, "name": name, "input": tool_input}
                            )
        except LlmProxyError as exc:
            return str(exc)
        return turn.error_message

    async def _run_tools(
        self, tool_uses: list[ToolUseComplete], *, message_id: uuid.UUID, guard: GenerationGuard
    ) -> tuple[list[dict], bool]:
        """Execute a turn's tool calls in order, recording each in `tool_call_events`.

        Returns the `tool_result` blocks (one per call, in call order) and whether the generation was
        cancelled meanwhile -- in which case the unfinished calls got a synthetic error result.
        """
        event_ids = {tool_use.id: uuid.uuid4() for tool_use in tool_uses}
        results: list[dict] = []

        async def record(tool_use: ToolUseComplete, result: ToolResult | None = None) -> None:
            await self.store.record_tool_event(
                event_ids[tool_use.id],
                session_id=self.session_id,
                message_id=message_id,
                tool_name=tool_use.name,
                tool_input=tool_use.input,
                result=result,
            )

        async def finish(tool_use: ToolUseComplete, result: ToolResult) -> None:
            await record(tool_use, result)
            self.manager.publish_event(
                self.session_id,
                "tool_result",
                {"id": tool_use.id, "content": result.content, "is_error": result.is_error},
            )
            results.append(_tool_result_block(tool_use.id, result))

        try:
            for tool_use in tool_uses:
                await record(tool_use)
                if tool_use.input_error:
                    result = ToolResult(MALFORMED_INPUT_MESSAGE, is_error=True)
                else:
                    result = await self.tools.execute(tool_use.name, tool_use.input, guard=guard)
                await finish(tool_use, result)
        except asyncio.CancelledError:
            # Not re-awaiting the cancelled tool: the pending calls are answered from here instead.
            cancelled = ToolResult(CANCELLED_TOOL_MESSAGE, is_error=True)
            for tool_use in tool_uses[len(results) :]:
                await finish(tool_use, cancelled)
            return results, True
        return results, False


def _tool_result_block(tool_use_id: str, result: ToolResult) -> dict:
    return {"type": "tool_result", "tool_use_id": tool_use_id, "content": result.content, "is_error": result.is_error}
