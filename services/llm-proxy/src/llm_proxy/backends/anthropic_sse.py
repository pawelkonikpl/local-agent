"""The Anthropic SSE writer and small helpers used by the Responses API translator
(`openai_responses_translate`)."""

import json
import uuid

from llm_proxy.usage import UsageAccumulator


def sse(event: str, data: dict) -> bytes:
    return f"event: {event}\ndata: {json.dumps(data)}\n\n".encode()


def extract_text(content: str | list[dict] | None) -> str:
    if content is None:
        return ""
    if isinstance(content, str):
        return content
    return "".join(block.get("text", "") for block in content if block.get("type") == "text")


def reasoning_effort(payload: dict) -> str | None:
    """Anthropic's `output_config.effort`, the one reasoning knob every backend understands.
    Passed through verbatim -- api only offers a model the levels its provider accepts."""
    return (payload.get("output_config") or {}).get("effort")


class AnthropicStreamWriter:
    """Emits Anthropic Messages SSE events for a stream translated from another provider.

    Anthropic content blocks are opened lazily and numbered sequentially (0, 1, 2...),
    independent of the provider's own indices; opening a new block closes the previous one
    first. Subclasses feed it provider events and set `stop_reason` as they learn it.
    """

    def __init__(self, model: str, usage: UsageAccumulator) -> None:
        self._model = model
        self._usage = usage
        self._next_index = 0
        self._open_index: int | None = None
        self._open_is_text = False
        self.stop_reason = "end_turn"

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
                    "delta": {"stop_reason": self.stop_reason, "stop_sequence": None},
                    "usage": {"output_tokens": self._usage.output_tokens},
                },
            )
        )
        events.append(sse("message_stop", {"type": "message_stop"}))
        return events

    def _text_delta(self, text: str) -> list[bytes]:
        events: list[bytes] = []
        if self._open_index is None or not self._open_is_text:
            events += self._open_block({"type": "text", "text": ""}, is_text=True)
        events.append(self._delta(self._open_index, {"type": "text_delta", "text": text}))
        return events

    def _open_tool_use(self, tool_id: str | None, name: str | None) -> list[bytes]:
        # The id must round-trip unchanged (tool_use.id -> tool_result.tool_use_id -> the
        # provider's call id), so only fall back to a generated one if the provider sent none.
        block = {"type": "tool_use", "id": tool_id or f"call_{uuid.uuid4().hex}", "name": name or "", "input": {}}
        return self._open_block(block, is_text=False)

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
