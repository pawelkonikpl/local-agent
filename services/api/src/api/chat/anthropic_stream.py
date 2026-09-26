"""Accumulates one model turn from llm-proxy's Anthropic-shaped SSE events.

Pure state machine -- no HTTP, no DB -- so the chat loop in `streaming.py` only has to feed it
parsed events and act on what it returns.
"""

import json
from dataclasses import dataclass, field

MALFORMED_INPUT_MESSAGE = "Invalid tool input: malformed JSON"


@dataclass(frozen=True)
class TextDelta:
    text: str


@dataclass(frozen=True)
class ToolUseComplete:
    """A `tool_use` block whose `content_block_stop` arrived, i.e. with its full input."""

    id: str
    name: str
    input: dict
    # The model's input JSON didn't parse: the call is recorded with `input={}` and answered
    # with an error result, never executed.
    input_error: bool = False


TurnEvent = TextDelta | ToolUseComplete


@dataclass
class _Block:
    type: str
    text: str = ""
    tool_id: str = ""
    tool_name: str = ""
    json_buffer: str = ""
    input: dict = field(default_factory=dict)
    input_error: bool = False
    # A `tool_use` only counts once `content_block_stop` arrived -- before that its input may be
    # incomplete (stream cut off, `max_tokens` hit mid-block).
    is_complete: bool = False


class TurnAccumulator:
    def __init__(self) -> None:
        self._blocks: dict[int, _Block] = {}
        self.stop_reason: str | None = None
        self.error_message: str | None = None

    def feed(self, event_type: str, data: dict) -> list[TurnEvent]:
        match event_type:
            case "content_block_start":
                self._on_block_start(data)
            case "content_block_delta":
                return self._on_block_delta(data)
            case "content_block_stop":
                return self._on_block_stop(data)
            case "message_delta":
                self.stop_reason = data.get("delta", {}).get("stop_reason") or self.stop_reason
            case "error":
                self.error_message = data.get("error", {}).get("message", "Upstream error")
        return []

    @property
    def content_blocks(self) -> list[dict]:
        """The turn's blocks in index order, in Anthropic format, ready to persist.

        Empty text blocks and incomplete `tool_use` blocks are left out.
        """
        blocks: list[dict] = []
        for _, block in sorted(self._blocks.items()):
            if block.type == "text" and block.text:
                blocks.append({"type": "text", "text": block.text})
            elif block.type == "tool_use" and block.is_complete:
                blocks.append({"type": "tool_use", "id": block.tool_id, "name": block.tool_name, "input": block.input})
        return blocks

    @property
    def text_blocks(self) -> list[dict]:
        """`content_blocks` without any `tool_use` -- what's kept when a turn is interrupted."""
        return [block for block in self.content_blocks if block["type"] == "text"]

    @property
    def tool_uses(self) -> list[ToolUseComplete]:
        return [
            ToolUseComplete(block.tool_id, block.tool_name, block.input, block.input_error)
            for _, block in sorted(self._blocks.items())
            if block.type == "tool_use" and block.is_complete
        ]

    def _on_block_start(self, data: dict) -> None:
        content_block = data.get("content_block", {})
        block_type = content_block.get("type", "")
        block = _Block(type=block_type)
        if block_type == "text":
            block.text = content_block.get("text", "")
        elif block_type == "tool_use":
            block.tool_id = content_block.get("id", "")
            block.tool_name = content_block.get("name", "")
        self._blocks[data.get("index", len(self._blocks))] = block

    def _on_block_delta(self, data: dict) -> list[TurnEvent]:
        delta = data.get("delta", {})
        index = data.get("index", 0)
        match delta.get("type"):
            case "text_delta":
                text = delta.get("text", "")
                if not text:
                    return []
                # Some upstreams skip `content_block_start` for plain text; treat it as implicit.
                block = self._blocks.setdefault(index, _Block(type="text"))
                block.text += text
                return [TextDelta(text)]
            case "input_json_delta":
                block = self._blocks.get(index)
                if block is not None and block.type == "tool_use":
                    block.json_buffer += delta.get("partial_json", "")
        return []

    def _on_block_stop(self, data: dict) -> list[TurnEvent]:
        block = self._blocks.get(data.get("index", 0))
        if block is None or block.type != "tool_use":
            return []
        block.is_complete = True
        if block.json_buffer.strip():
            try:
                parsed = json.loads(block.json_buffer)
            except json.JSONDecodeError:
                parsed = None
            if isinstance(parsed, dict):
                block.input = parsed
            else:
                block.input_error = True
        return [ToolUseComplete(block.tool_id, block.tool_name, block.input, block.input_error)]
