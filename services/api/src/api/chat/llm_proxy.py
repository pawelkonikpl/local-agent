"""api's client of llm-proxy (`contracts.llm_proxy`): the one place that knows its paths, headers
and error bodies."""

import json
import uuid
from collections.abc import AsyncIterator

import httpx
from contracts.auth import bearer_headers
from contracts.llm_proxy import (
    MESSAGES_PATH,
    MODELS_PATH,
    SESSION_ID_HEADER,
    USER_ID_HEADER,
    ModelInfo,
    ModelsResponse,
)
from contracts.sse import iter_sse
from pydantic import ValidationError


class LlmProxyError(Exception):
    """A model turn couldn't be streamed; the message is shown to the user as the generation's error."""


class ModelCatalogUnavailableError(Exception):
    """llm-proxy couldn't say which models its providers serve."""


class LlmProxy:
    def __init__(self, client: httpx.AsyncClient, token: str) -> None:
        self._client = client
        self._token = token

    async def list_models(self) -> list[ModelInfo]:
        """The models llm-proxy's providers serve (it asks each provider and caches the answer)."""
        try:
            response = await self._client.get(MODELS_PATH, headers=bearer_headers(self._token))
            response.raise_for_status()
            models = ModelsResponse.model_validate_json(response.content).models
        except (httpx.HTTPError, ValidationError) as exc:
            raise ModelCatalogUnavailableError(f"Could not list models from llm-proxy: {exc}") from exc
        if not models:
            raise ModelCatalogUnavailableError("llm-proxy listed no models")
        return models

    async def stream_turn(
        self, payload: dict, *, user_id: uuid.UUID, session_id: uuid.UUID
    ) -> AsyncIterator[tuple[str, dict]]:
        """One model turn as Anthropic Messages SSE `(event, data)` pairs.

        Raises `LlmProxyError` if llm-proxy rejects the request or can't be reached.
        """
        headers = bearer_headers(self._token) | {USER_ID_HEADER: str(user_id), SESSION_ID_HEADER: str(session_id)}
        try:
            async with self._client.stream("POST", MESSAGES_PATH, json=payload, headers=headers) as response:
                if response.status_code >= 400:
                    raise LlmProxyError(_error_detail(await response.aread(), response.status_code))
                async for event in iter_sse(response.aiter_lines()):
                    yield event
        except httpx.HTTPError as exc:
            raise LlmProxyError(f"llm-proxy unreachable: {exc}") from exc


def _error_detail(body: bytes, status_code: int) -> str:
    """FastAPI's `{"detail": ...}` of an error response, or just its status."""
    try:
        parsed = json.loads(body)
    except json.JSONDecodeError:
        parsed = None
    if isinstance(parsed, dict) and parsed.get("detail"):
        return str(parsed["detail"])
    return f"llm-proxy returned {status_code}"
