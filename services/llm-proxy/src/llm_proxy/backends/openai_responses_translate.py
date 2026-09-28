"""Anthropic Messages <-> OpenAI Responses API translation for `OpenAIResponsesBackend`.

Kept as pure functions plus one small stream-state class, so both directions can be unit
tested without any HTTP in the loop.
"""

import json

from contracts.sse import encode_sse
from openai.types.responses import ResponseStreamEvent, ResponseUsage

from llm_proxy.backends.anthropic_sse import (
    AnthropicStreamWriter,
    extract_text,
    reasoning_effort,
)
from llm_proxy.usage import UsageAccumulator


def _to_function_call_output(block: dict) -> dict:
    output = extract_text(block.get("content"))
    if block.get("is_error"):
        output = f"Error: {output}"
    return {"type": "function_call_output", "call_id": block["tool_use_id"], "output": output}


def _to_function_call(block: dict) -> dict:
    return {
        "type": "function_call",
        "call_id": block["id"],
        "name": block["name"],
        "arguments": json.dumps(block.get("input", {})),
    }


def _translate_user(content: str | list[dict]) -> list[dict]:
    if isinstance(content, str):
        return [{"role": "user", "content": content}]
    # Tool results answer the previous assistant turn's calls, so they go ahead of any user text
    # from the same Anthropic message.
    items = [_to_function_call_output(block) for block in content if block.get("type") == "tool_result"]
    text = extract_text(content)
    if text or not items:
        items.append({"role": "user", "content": text})
    return items


def _translate_assistant(content: str | list[dict]) -> list[dict]:
    if isinstance(content, str):
        return [{"role": "assistant", "content": content}]
    items: list[dict] = []
    text = extract_text(content)
    if text:
        items.append({"role": "assistant", "content": text})
    items += [_to_function_call(block) for block in content if block.get("type") == "tool_use"]
    return items


def to_responses_input(payload: dict) -> list[dict]:
    """The Anthropic `messages` as Responses `input` items (the system prompt goes to
    `instructions` separately, see `to_responses_kwargs`)."""
    items: list[dict] = []
    for message in payload.get("messages", []):
        if message["role"] == "assistant":
            items += _translate_assistant(message["content"])
        else:
            items += _translate_user(message["content"])
    return items


def _to_function_tool(tool: dict) -> dict:
    function_tool = {
        "type": "function",
        "name": tool["name"],
        "parameters": tool.get("input_schema", {"type": "object"}),
        # Responses defaults to strict schemas (every property required, no additional
        # properties), which arbitrary Anthropic `input_schema`s don't satisfy.
        "strict": False,
    }
    if tool.get("description"):
        function_tool["description"] = tool["description"]
    return function_tool


def _to_tool_choice(tool_choice: dict) -> str | dict:
    choice_type = tool_choice.get("type")
    if choice_type == "any":
        return "required"
    if choice_type == "none":
        return "none"
    if choice_type == "tool":
        return {"type": "function", "name": tool_choice["name"]}
    return "auto"


def to_responses_kwargs(payload: dict) -> dict:
    """`instructions` / `reasoning` / `tools` / `tool_choice` kwargs for `responses.create`, each
    present only when the Anthropic request carries the matching field."""
    kwargs: dict = {}
    system = payload.get("system")
    if system:
        kwargs["instructions"] = extract_text(system)
    effort = reasoning_effort(payload)
    if effort:
        kwargs["reasoning"] = {"effort": effort}
    tools = payload.get("tools")
    if tools:
        kwargs["tools"] = [_to_function_tool(tool) for tool in tools]
        tool_choice = payload.get("tool_choice")
        if tool_choice:
            kwargs["tool_choice"] = _to_tool_choice(tool_choice)
    return kwargs


class ResponsesStreamTranslator(AnthropicStreamWriter):
    """Turns streamed Responses API events into Anthropic Messages SSE events.

    Only `message` text (refusals included) and `function_call` items become Anthropic blocks;
    `reasoning` items stay hidden -- their tokens still count, via the final usage, as output.
    """

    def __init__(self, model: str, usage: UsageAccumulator) -> None:
        super().__init__(model, usage)
        # Responses `output_index` -> Anthropic block index, for function calls.
        self._tool_blocks: dict[int, int] = {}

    def on_event(self, event: ResponseStreamEvent) -> list[bytes]:
        match event.type:
            case "response.output_item.added" if event.item.type == "function_call":
                events = self._open_tool_use(event.item.call_id, event.item.name)
                self._tool_blocks[event.output_index] = self._open_index
                self.stop_reason = "tool_use"
                return events
            case "response.function_call_arguments.delta":
                block_index = self._tool_blocks.get(event.output_index)
                if block_index is None or not event.delta:
                    return []
                return [self._delta(block_index, {"type": "input_json_delta", "partial_json": event.delta})]
            case "response.output_text.delta" | "response.refusal.delta":
                return self._text_delta(event.delta) if event.delta else []
            case "response.completed" | "response.incomplete":
                self._record_usage(event.response.usage)
                details = event.response.incomplete_details
                if details is not None and details.reason == "max_output_tokens":
                    self.stop_reason = "max_tokens"
            case "response.failed":
                self._record_usage(event.response.usage)
                error = event.response.error
                return [self._error(error.message if error else "Upstream response failed")]
            case "error":
                return [self._error(event.message)]
        return []

    def _record_usage(self, usage: ResponseUsage | None) -> None:
        if usage is not None:
            self._usage.input_tokens = usage.input_tokens
            # Includes the hidden reasoning tokens, which OpenAI bills as output.
            self._usage.output_tokens = usage.output_tokens

    @staticmethod
    def _error(message: str) -> bytes:
        return encode_sse("error", {"type": "error", "error": {"type": "api_error", "message": message}})
