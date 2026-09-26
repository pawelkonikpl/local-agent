"""OpenAI Chat Completions request -> Qwen2.5 chat-template input.

Pure Python (no torch/transformers import), so it's testable without loading any weights.
"""

import json


def _extract_text(content: str | list[dict] | None) -> str:
    if content is None:
        return ""
    if isinstance(content, str):
        return content
    return "".join(block.get("text", "") for block in content if block.get("type") == "text")


def _decode_arguments(arguments: str | dict) -> dict | str:
    # OpenAI sends `arguments` as a JSON string, but Qwen2.5's template renders them with
    # `arguments | tojson` -- passing the string through would double-encode it.
    if not isinstance(arguments, str):
        return arguments
    try:
        return json.loads(arguments)
    except json.JSONDecodeError:
        return arguments


def _to_template_tool_call(tool_call: dict) -> dict:
    function = tool_call.get("function", {})
    return {
        "type": "function",
        "function": {"name": function.get("name", ""), "arguments": _decode_arguments(function.get("arguments", {}))},
    }


def to_template_messages(messages: list[dict]) -> list[dict]:
    template_messages = []
    for message in messages:
        role = message.get("role")
        content = _extract_text(message.get("content"))
        if role in ("user", "system"):
            template_messages.append({"role": role, "content": content})
        elif role == "assistant":
            template_message = {"role": "assistant", "content": content}
            if message.get("tool_calls"):
                template_message["tool_calls"] = [_to_template_tool_call(tc) for tc in message["tool_calls"]]
            template_messages.append(template_message)
        elif role == "tool":
            template_messages.append({"role": "tool", "content": content})
    return template_messages


def to_template_tools(payload: dict) -> list[dict] | None:
    """`tools` in the `{"type": "function", "function": {...}}` shape Qwen2.5's function-calling
    prompt is built around, or None when there are none or `tool_choice` is `"none"`.
    `"required"` / a forced function can't be enforced on a model this small, so any other
    `tool_choice` is best-effort: the tools are offered and the model decides."""
    tools = payload.get("tools")
    if not tools or payload.get("tool_choice") == "none":
        return None
    return tools
