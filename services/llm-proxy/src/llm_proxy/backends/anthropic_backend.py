import json
from collections.abc import AsyncIterator

import httpx
from pydantic import BaseModel, ConfigDict

from llm_proxy.backends.base import ModelInfo, UpstreamError
from llm_proxy.usage import UsageAccumulator

ANTHROPIC_VERSION = "2023-06-01"
# The Models API's page size cap -- one page covers every model an org has today.
_MODELS_PAGE_LIMIT = 1000


class _Supported(BaseModel):
    supported: bool = False


class _EffortCapability(BaseModel):
    """`capabilities.effort`: its own `supported` flag plus one `{"supported": bool}` entry per
    level (`low`, `medium`, ...), weakest first. Levels are read from the response, not listed
    here, so a level Anthropic adds later shows up without a code change."""

    model_config = ConfigDict(extra="allow")

    supported: bool = False

    def supported_levels(self) -> list[str]:
        if not self.supported:
            return []
        return [
            level
            for level, value in (self.model_extra or {}).items()
            if isinstance(value, dict) and _Supported.model_validate(value).supported
        ]


class _Capabilities(BaseModel):
    effort: _EffortCapability = _EffortCapability()


class _AnthropicModel(BaseModel):
    id: str
    capabilities: _Capabilities = _Capabilities()


class _AnthropicModelsPage(BaseModel):
    data: list[_AnthropicModel]
    has_more: bool = False
    last_id: str | None = None


def build_anthropic_client(base_url: str) -> httpx.AsyncClient:
    return httpx.AsyncClient(base_url=base_url, timeout=httpx.Timeout(10.0, read=None))


class AnthropicBackend:
    """Real Claude. Pure passthrough to `api.anthropic.com` -- upstream already speaks the
    wire format this proxy emits, so bytes are forwarded as-is (re-framed line by line, not
    necessarily byte-identical to what Anthropic sent)."""

    def __init__(self, client: httpx.AsyncClient, api_key: str) -> None:
        self._client = client
        self._api_key = api_key

    def _headers(self) -> dict[str, str]:
        return {
            "x-api-key": self._api_key,
            "anthropic-version": ANTHROPIC_VERSION,
            "content-type": "application/json",
        }

    async def list_models(self) -> list[ModelInfo]:
        """Every model the API key can use (`GET /v1/models`, newest first), with the effort
        levels its `capabilities` report."""
        models: list[ModelInfo] = []
        params: dict[str, str | int] = {"limit": _MODELS_PAGE_LIMIT}
        while True:
            try:
                response = await self._client.get("/v1/models", params=params, headers=self._headers())
            except httpx.HTTPError as exc:
                raise UpstreamError(502, str(exc)) from exc
            if response.status_code >= 400:
                raise UpstreamError(response.status_code, response.text)
            page = _AnthropicModelsPage.model_validate(response.json())
            models.extend(
                ModelInfo(id=model.id, reasoning_efforts=model.capabilities.effort.supported_levels())
                for model in page.data
            )
            if not page.has_more or page.last_id is None:
                return models
            params = {"limit": _MODELS_PAGE_LIMIT, "after_id": page.last_id}

    async def stream(self, payload: dict, usage: UsageAccumulator) -> AsyncIterator[bytes]:
        request_body = {**payload, "stream": True}
        async with self._client.stream(
            "POST", "/v1/messages", json=request_body, headers=self._headers()
        ) as response:
            if response.status_code >= 400:
                body = await response.aread()
                raise UpstreamError(response.status_code, body.decode("utf-8", errors="replace"))

            event_type: str | None = None
            async for line in response.aiter_lines():
                if line.startswith("event:"):
                    event_type = line[len("event:") :].strip()
                    yield f"{line}\n".encode()
                elif line.startswith("data:"):
                    raw_data = line[len("data:") :].strip()
                    if event_type is not None and raw_data:
                        try:
                            usage.observe(event_type, json.loads(raw_data))
                        except json.JSONDecodeError:
                            pass
                    yield f"{line}\n".encode()
                else:
                    yield b"\n"
