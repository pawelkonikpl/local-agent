from collections.abc import Sequence

from shared.db.models import Message

INTERRUPTED_TOOL_MESSAGE = "Tool execution was interrupted"


def build_llm_messages(messages: Sequence[Message]) -> list[dict]:
    """Turn persisted session messages into a valid Messages API `messages` list.

    The Messages API requires roles to alternate and every `tool_use` to be answered by a
    `tool_result` in the very next message. Persisted history can break both -- a generation that
    failed without text leaves `user, user`; hitting the tool iteration limit ends on
    `user(tool_result)` before the next `user(text)`; a crash between saving a `tool_use` and its
    result leaves it orphaned -- so this repairs the list sent to the model, never the rows.
    """
    merged: list[dict] = []
    for message in messages:
        if message.role not in ("user", "assistant"):
            continue
        if merged and merged[-1]["role"] == message.role:
            merged[-1]["content"] = [*merged[-1]["content"], *message.content]
        else:
            merged.append({"role": message.role, "content": list(message.content)})
    return _answer_orphaned_tool_uses(merged)


def _answer_orphaned_tool_uses(messages: list[dict]) -> list[dict]:
    repaired: list[dict] = []
    for index, message in enumerate(messages):
        if message["role"] == "user" and repaired and repaired[-1]["role"] == "assistant":
            message = _with_missing_results(message, _tool_use_ids(repaired[-1]))
        repaired.append(message)
        is_last = index == len(messages) - 1
        if message["role"] == "assistant" and is_last and _tool_use_ids(message):
            # Callers normally pass the new user turn too, so this is only a fallback for a history
            # ending on an unanswered `tool_use`.
            repaired.append(_with_missing_results({"role": "user", "content": []}, _tool_use_ids(message)))
    return repaired


def _with_missing_results(user_message: dict, tool_use_ids: list[str]) -> dict:
    answered = {block.get("tool_use_id") for block in user_message["content"] if block.get("type") == "tool_result"}
    missing = [
        {"type": "tool_result", "tool_use_id": tool_use_id, "content": INTERRUPTED_TOOL_MESSAGE, "is_error": True}
        for tool_use_id in tool_use_ids
        if tool_use_id not in answered
    ]
    # The API expects `tool_result` blocks ahead of any text in a user message.
    results = [block for block in user_message["content"] if block.get("type") == "tool_result"]
    others = [block for block in user_message["content"] if block.get("type") != "tool_result"]
    return {"role": "user", "content": [*missing, *results, *others]}


def _tool_use_ids(message: dict) -> list[str]:
    return [block["id"] for block in message["content"] if block.get("type") == "tool_use"]
