from collections.abc import AsyncIterator, Sequence
from typing import Protocol

from contracts.llm_proxy import ModelInfo

from llm_proxy.usage import UsageAccumulator


class UpstreamError(Exception):
    """The upstream provider rejected the request before any bytes were streamed back."""

    def __init__(self, status_code: int, message: str) -> None:
        super().__init__(message)
        self.status_code = status_code


class NoBackendError(Exception):
    """No configured provider serves the requested model."""

    def __init__(self, model: str) -> None:
        super().__init__(f"No configured provider serves model {model!r}")


class LLMBackend(Protocol):
    """Whatever wire protocol the upstream provider speaks, `stream` always yields raw
    Anthropic Messages API SSE lines (`event: ...` / `data: ...`) -- the one contract api
    understands, regardless of which provider actually served the request. Raises
    `UpstreamError` if the upstream rejects the request before any bytes are streamed back.

    Tool calling is part of the contract, implemented natively per provider: a backend must
    accept `tools` / `tool_choice` and `tool_use` / `tool_result` blocks in `messages`, and
    emit a model's tool call as a `tool_use` block -- `content_block_start` with
    `{"type": "tool_use", "id", "name", "input": {}}`, then `content_block_delta` events with
    `input_json_delta` -- ending the message with `stop_reason: "tool_use"`.

    Reasoning depth likewise travels as Anthropic's `output_config.effort`; a backend maps it
    onto its provider's own knob (or passes it through, for Claude).

    `serves` says whether a model id belongs to this provider, so requests are routed without a
    central list of prefixes. `list_models` asks the provider which models it serves right now
    (and which effort levels each accepts), so no model list is hardcoded on either side of the
    proxy. Raises `UpstreamError` if the provider can't be asked.
    """

    def serves(self, model: str) -> bool: ...

    def stream(self, payload: dict, usage: UsageAccumulator) -> AsyncIterator[bytes]: ...

    async def list_models(self) -> list[ModelInfo]: ...


class BackendRouter:
    """The configured backends, in priority order: the first one that `serves` a model gets it."""

    def __init__(self, backends: Sequence[LLMBackend]) -> None:
        self.backends = tuple(backends)

    def for_model(self, model: str) -> LLMBackend:
        for backend in self.backends:
            if backend.serves(model):
                return backend
        raise NoBackendError(model)
