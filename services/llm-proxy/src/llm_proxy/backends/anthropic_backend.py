import json
from collections.abc import AsyncIterator

import httpx

from llm_proxy.backends.base import UpstreamError
from llm_proxy.usage import UsageAccumulator

ANTHROPIC_VERSION = "2023-06-01"


def build_anthropic_client(base_url: str) -> httpx.AsyncClient:
    return httpx.AsyncClient(base_url=base_url, timeout=httpx.Timeout(10.0, read=None))


class AnthropicBackend:
    """Real Claude. Pure passthrough to `api.anthropic.com` -- upstream already speaks the
    wire format this proxy emits, so bytes are forwarded as-is (re-framed line by line, not
    necessarily byte-identical to what Anthropic sent)."""

    def __init__(self, client: httpx.AsyncClient, api_key: str) -> None:
        self._client = client
        self._api_key = api_key

    async def stream(self, payload: dict, usage: UsageAccumulator) -> AsyncIterator[bytes]:
        request_body = {**payload, "stream": True}
        headers = {
            "x-api-key": self._api_key,
            "anthropic-version": ANTHROPIC_VERSION,
            "content-type": "application/json",
        }
        async with self._client.stream(
            "POST", "/v1/messages", json=request_body, headers=headers
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
