from collections.abc import AsyncIterator
from typing import Protocol

from llm_proxy.usage import UsageAccumulator


class UpstreamError(Exception):
    """The upstream provider rejected the request before any bytes were streamed back."""

    def __init__(self, status_code: int, message: str) -> None:
        super().__init__(message)
        self.status_code = status_code


class LLMBackend(Protocol):
    """Whatever wire protocol the upstream provider speaks, `stream` always yields raw
    Anthropic Messages API SSE lines (`event: ...` / `data: ...`) -- the one contract
    session-agent's `anthropic` SDK understands, regardless of which provider actually served
    the request. Raises `UpstreamError` if the upstream rejects the request before any bytes
    are streamed back.

    Tool calling is part of the contract, implemented natively per provider: a backend must
    accept `tools` / `tool_choice` and `tool_use` / `tool_result` blocks in `messages`, and
    emit a model's tool call as a `tool_use` block -- `content_block_start` with
    `{"type": "tool_use", "id", "name", "input": {}}`, then `content_block_delta` events with
    `input_json_delta` -- ending the message with `stop_reason: "tool_use"`.
    """

    def stream(self, payload: dict, usage: UsageAccumulator) -> AsyncIterator[bytes]: ...
