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
    """

    def stream(self, payload: dict, usage: UsageAccumulator) -> AsyncIterator[bytes]: ...
