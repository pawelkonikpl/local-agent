from collections.abc import AsyncIterator
from typing import Protocol

from pydantic import BaseModel

from llm_proxy.usage import UsageAccumulator


class UpstreamError(Exception):
    """The upstream provider rejected the request before any bytes were streamed back."""

    def __init__(self, status_code: int, message: str) -> None:
        super().__init__(message)
        self.status_code = status_code


class ModelInfo(BaseModel):
    """A model a provider serves, as offered to api's model picker."""

    id: str
    # Levels of Anthropic's `output_config.effort` the model accepts; empty = no reasoning setting.
    reasoning_efforts: list[str] = []


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

    Reasoning depth likewise travels as Anthropic's `output_config.effort`; a backend maps it
    onto its provider's own knob (or passes it through, for Claude).

    `list_models` asks the provider which models it serves right now (and which effort levels
    each accepts), so no model list is hardcoded on either side of the proxy. Raises
    `UpstreamError` if the provider can't be asked.
    """

    def stream(self, payload: dict, usage: UsageAccumulator) -> AsyncIterator[bytes]: ...

    async def list_models(self) -> list[ModelInfo]: ...
