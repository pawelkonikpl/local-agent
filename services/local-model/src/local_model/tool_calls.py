"""Streaming parser for Qwen2.5's native tool-call output format.

Given `tools`, Qwen2.5's chat template tells the model to answer with
`<tool_call>\\n{"name": ..., "arguments": {...}}\\n</tool_call>` blocks. This module splits the
generated text stream into plain text and parsed tool calls. Pure Python on purpose -- no
torch/transformers import, so it's testable without loading any weights.
"""

import json
import uuid
from dataclasses import dataclass

TOOL_CALL_OPEN = "<tool_call>"
TOOL_CALL_CLOSE = "</tool_call>"


@dataclass(frozen=True)
class ToolCall:
    name: str
    arguments: dict


def _parse_tool_call(body: str) -> ToolCall | None:
    try:
        data = json.loads(body)
    except json.JSONDecodeError:
        return None
    if not isinstance(data, dict) or not isinstance(data.get("name"), str):
        return None
    arguments = data.get("arguments", {})
    if isinstance(arguments, str):
        try:
            arguments = json.loads(arguments)
        except json.JSONDecodeError:
            return None
    if not isinstance(arguments, dict):
        return None
    return ToolCall(name=data["name"], arguments=arguments)


def _partial_open_tag_length(text: str) -> int:
    """Length of the longest suffix of `text` that could still grow into `<tool_call>`."""
    for length in range(min(len(text), len(TOOL_CALL_OPEN) - 1), 0, -1):
        if TOOL_CALL_OPEN.startswith(text[-length:]):
            return length
    return 0


class ToolCallStreamParser:
    """Feed generated text chunks in, get back a list of `str` (text to stream as-is) and
    `ToolCall` items. Text is held back only while it might be the start of a `<tool_call>`
    tag split across chunks; everything inside a tag is buffered until `</tool_call>` or the
    end of generation. A tag whose body isn't a valid tool-call JSON comes back as plain text.
    """

    def __init__(self) -> None:
        self._pending = ""
        self._in_tool_call = False
        self._after_tool_call = False

    def feed(self, chunk: str) -> list[str | ToolCall]:
        self._pending += chunk
        items: list[str | ToolCall] = []
        while True:
            if self._in_tool_call:
                close_at = self._pending.find(TOOL_CALL_CLOSE)
                if close_at == -1:
                    return items
                body = self._pending[:close_at]
                self._pending = self._pending[close_at + len(TOOL_CALL_CLOSE) :]
                self._in_tool_call = False
                items.append(self._close_tool_call(body, closed=True))
                continue

            if self._after_tool_call:
                # Drop the newlines Qwen puts between/after consecutive tool calls, so they don't
                # surface as stray whitespace-only text blocks.
                self._pending = self._pending.lstrip()
                if not self._pending:
                    return items
                self._after_tool_call = self._pending.startswith(TOOL_CALL_OPEN[: len(self._pending)])

            open_at = self._pending.find(TOOL_CALL_OPEN)
            if open_at != -1:
                if open_at:
                    items.append(self._pending[:open_at])
                self._pending = self._pending[open_at + len(TOOL_CALL_OPEN) :]
                self._in_tool_call = True
                continue

            keep = _partial_open_tag_length(self._pending)
            text = self._pending[: len(self._pending) - keep]
            self._pending = self._pending[len(self._pending) - keep :]
            if text:
                items.append(text)
            return items

    def finish(self) -> list[str | ToolCall]:
        """Flush at end of generation -- an unterminated `<tool_call>` is still parsed if its
        body is complete JSON (the model may stop right before emitting `</tool_call>`)."""
        pending, self._pending = self._pending, ""
        if self._in_tool_call:
            self._in_tool_call = False
            return [self._close_tool_call(pending, closed=False)]
        if self._after_tool_call and not pending.strip():
            return []
        return [pending] if pending else []

    def _close_tool_call(self, body: str, *, closed: bool) -> str | ToolCall:
        tool_call = _parse_tool_call(body.strip())
        if tool_call is None:
            return TOOL_CALL_OPEN + body + (TOOL_CALL_CLOSE if closed else "")
        self._after_tool_call = True
        return tool_call


class ChatCompletionDeltas:
    """Generated text chunks in -> OpenAI `chat.completion.chunk` deltas out: plain text as
    `{"content": ...}`, each parsed tool call as `{"tool_calls": [...]}` with its arguments in a
    single fragment."""

    def __init__(self) -> None:
        self._parser = ToolCallStreamParser()
        self._tool_call_count = 0

    @property
    def finish_reason(self) -> str:
        return "tool_calls" if self._tool_call_count else "stop"

    def feed(self, chunk: str) -> list[dict]:
        return [self._to_delta(item) for item in self._parser.feed(chunk)]

    def finish(self) -> list[dict]:
        return [self._to_delta(item) for item in self._parser.finish()]

    def _to_delta(self, item: str | ToolCall) -> dict:
        if isinstance(item, str):
            return {"content": item}
        tool_call = {
            "index": self._tool_call_count,
            "id": f"call_{uuid.uuid4().hex}",
            "type": "function",
            "function": {"name": item.name, "arguments": json.dumps(item.arguments)},
        }
        self._tool_call_count += 1
        return {"tool_calls": [tool_call]}
