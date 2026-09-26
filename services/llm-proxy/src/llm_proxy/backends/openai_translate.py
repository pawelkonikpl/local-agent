"""Anthropic Messages <-> OpenAI Chat Completions translation for `OpenAIBackend`.

Kept as pure functions plus one small stream-state class, so both directions can be unit
tested without any HTTP in the loop.
"""

import json
import uuid

from openai.types.chat import ChatCompletionChunk
from openai.types.chat.chat_completion_chunk import ChoiceDeltaToolCall

from llm_proxy.usage import UsageAccumulator

_FINISH_TO_STOP_REASON = {"tool_calls": "tool_use", "length": "max_tokens"}


def sse(event: str, data: dict) -> bytes:
    return f"event: {event}\ndata: {json.dumps(data)}\n\n".encode()


def _extract_text(content: str | list[dict] | None) -> str:
    if content is None:
        return ""
    if isinstance(content, str):
        return content
    return "".join(block.get("text", "") for block in content if block.get("type") == "text")


def _to_tool_message(block: dict) -> dict:
    content = _extract_text(block.get("content"))
    if block.get("is_error"):
        content = f"Error: {content}"
    return {"role": "tool", "tool_call_id": block["tool_use_id"], "content": content}


def _to_tool_call(block: dict) -> dict:
    return {
        "id": block["id"],
        "type": "function",
        "function": {"name": block["name"], "arguments": json.dumps(block.get("input", {}))},
    }


def _translate_user(content: str | list[dict]) -> list[dict]:
    if isinstance(content, str):
        return [{"role": "user", "content": content}]
    # OpenAI requires `tool` messages to directly follow the assistant's `tool_calls`, so tool
    # results go first and any user text from the same Anthropic message comes after them.
    messages = [_to_tool_message(block) for block in content if block.get("type") == "tool_result"]
    text = _extract_text(content)
    if text or not messages:
        messages.append({"role": "user", "content": text})
    return messages


def _translate_assistant(content: str | list[dict]) -> dict:
    if isinstance(content, str):
        return {"role": "assistant", "content": content}
    text = _extract_text(content)
    tool_calls = [_to_tool_call(block) for block in content if block.get("type") == "tool_use"]
    if not tool_calls:
        return {"role": "assistant", "content": text}
    return {"role": "assistant", "content": text or None, "tool_calls": tool_calls}


def to_openai_messages(payload: dict) -> list[dict]:
    messages: list[dict] = []
    system = payload.get("system")
    if system:
        messages.append({"role": "system", "content": _extract_text(system)})
    for message in payload.get("messages", []):
        if message["role"] == "assistant":
            messages.append(_translate_assistant(message["content"]))
        else:
            messages.extend(_translate_user(message["content"]))
    return messages


def _to_openai_tool(tool: dict) -> dict:
    function = {"name": tool["name"], "parameters": tool.get("input_schema", {"type": "object"})}
    if tool.get("description"):
        function["description"] = tool["description"]
    return {"type": "function", "function": function}


def _to_openai_tool_choice(tool_choice: dict) -> str | dict:
    choice_type = tool_choice.get("type")
    if choice_type == "any":
        return "required"
    if choice_type == "none":
        return "none"
    if choice_type == "tool":
        return {"type": "function", "function": {"name": tool_choice["name"]}}
    return "auto"


def to_openai_tool_kwargs(payload: dict) -> dict:
    """`tools` / `tool_choice` kwargs for `chat.completions.create` -- empty when the request
    carries no tools, so plain chat requests stay exactly as they were."""
    tools = payload.get("tools")
    if not tools:
        return {}
    kwargs: dict = {"tools": [_to_openai_tool(tool) for tool in tools]}
    tool_choice = payload.get("tool_choice")
    if tool_choice:
        kwargs["tool_choice"] = _to_openai_tool_choice(tool_choice)
    return kwargs


class StreamTranslator:
    """Turns streamed chat-completion chunks into Anthropic Messages SSE events.

    Anthropic content blocks are opened lazily and numbered sequentially (0, 1, 2...),
    independent of OpenAI's own `tool_calls[].index`; switching from text to a tool call, or
    between tool calls, closes the previous block first.
    """

    def __init__(self, model: str, usage: UsageAccumulator) -> None:
        self._model = model
        self._usage = usage
        self._next_index = 0
        self._open_index: int | None = None
        self._open_is_text = False
        self._tool_blocks: dict[int, int] = {}
        self._stop_reason = "end_turn"

    def start(self) -> list[bytes]:
        return [
            sse(
                "message_start",
                {
                    "type": "message_start",
                    "message": {
                        "id": f"msg_{uuid.uuid4().hex}",
                        "type": "message",
                        "role": "assistant",
                        "model": self._model,
                        "content": [],
                        "stop_reason": None,
                        "stop_sequence": None,
                        "usage": {"input_tokens": 0, "output_tokens": 0},
                    },
                },
            )
        ]

    def on_chunk(self, chunk: ChatCompletionChunk) -> list[bytes]:
        events: list[bytes] = []
        if chunk.choices:
            choice = chunk.choices[0]
            delta = choice.delta
            if delta and delta.content:
                if self._open_index is None or not self._open_is_text:
                    events += self._open_block({"type": "text", "text": ""}, is_text=True)
                events.append(self._delta(self._open_index, {"type": "text_delta", "text": delta.content}))
            if delta and delta.tool_calls:
                for tool_call in delta.tool_calls:
                    events += self._on_tool_call_fragment(tool_call)
            if choice.finish_reason:
                self._stop_reason = _FINISH_TO_STOP_REASON.get(choice.finish_reason, "end_turn")
        if chunk.usage:
            self._usage.input_tokens = chunk.usage.prompt_tokens
            self._usage.output_tokens = chunk.usage.completion_tokens
        return events

    def finish(self) -> list[bytes]:
        events: list[bytes] = []
        if self._next_index == 0:
            # Never end with a content-less message -- the `anthropic` SDK expects at least one block.
            events += self._open_block({"type": "text", "text": ""}, is_text=True)
        events += self._close_open_block()
        events.append(
            sse(
                "message_delta",
                {
                    "type": "message_delta",
                    "delta": {"stop_reason": self._stop_reason, "stop_sequence": None},
                    "usage": {"output_tokens": self._usage.output_tokens},
                },
            )
        )
        events.append(sse("message_stop", {"type": "message_stop"}))
        return events

    def _on_tool_call_fragment(self, tool_call: ChoiceDeltaToolCall) -> list[bytes]:
        events: list[bytes] = []
        block_index = self._tool_blocks.get(tool_call.index)
        if block_index is None:
            name = tool_call.function.name if tool_call.function else None
            # The id must round-trip unchanged (tool_use.id -> tool_result.tool_use_id ->
            # tool_call_id), so only fall back to a generated one if the provider sent none.
            block = {
                "type": "tool_use",
                "id": tool_call.id or f"call_{uuid.uuid4().hex}",
                "name": name or "",
                "input": {},
            }
            events += self._open_block(block, is_text=False)
            block_index = self._open_index
            self._tool_blocks[tool_call.index] = block_index
        arguments = tool_call.function.arguments if tool_call.function else None
        if arguments:
            events.append(self._delta(block_index, {"type": "input_json_delta", "partial_json": arguments}))
        return events

    def _open_block(self, content_block: dict, *, is_text: bool) -> list[bytes]:
        events = self._close_open_block()
        self._open_index = self._next_index
        self._open_is_text = is_text
        self._next_index += 1
        events.append(
            sse(
                "content_block_start",
                {"type": "content_block_start", "index": self._open_index, "content_block": content_block},
            )
        )
        return events

    def _close_open_block(self) -> list[bytes]:
        if self._open_index is None:
            return []
        index, self._open_index = self._open_index, None
        return [sse("content_block_stop", {"type": "content_block_stop", "index": index})]

    @staticmethod
    def _delta(index: int, delta: dict) -> bytes:
        return sse("content_block_delta", {"type": "content_block_delta", "index": index, "delta": delta})
