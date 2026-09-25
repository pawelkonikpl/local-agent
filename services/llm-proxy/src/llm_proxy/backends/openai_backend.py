import json
import uuid
from collections.abc import AsyncIterator

import openai

from llm_proxy.backends.base import UpstreamError
from llm_proxy.usage import UsageAccumulator


def _sse(event: str, data: dict) -> bytes:
    return f"event: {event}\ndata: {json.dumps(data)}\n\n".encode()


def _extract_text(content: str | list[dict]) -> str:
    if isinstance(content, str):
        return content
    return "".join(block.get("text", "") for block in content if block.get("type") == "text")


def _to_openai_messages(payload: dict) -> list[dict]:
    messages = []
    system = payload.get("system")
    if system:
        messages.append({"role": "system", "content": _extract_text(system)})
    for message in payload.get("messages", []):
        messages.append({"role": message["role"], "content": _extract_text(message["content"])})
    return messages


class OpenAIBackend:
    """Any provider speaking OpenAI's Chat Completions format natively -- real OpenAI, or a
    self-hosted model exposing an OpenAI-compatible endpoint (this project's own `local-model`
    service included). Translates the streamed chat-completion chunks into Anthropic Messages
    SSE events, since that's the one contract session-agent's `anthropic` SDK understands.

    Text-only for now: no tool_calls -> tool_use translation (session-agent's tool loop is
    Etap 5, not built yet). Extend here when that lands.
    """

    def __init__(self, client: openai.AsyncOpenAI, model: str) -> None:
        self._client = client
        self._model = model

    async def stream(self, payload: dict, usage: UsageAccumulator) -> AsyncIterator[bytes]:
        usage.model = self._model
        try:
            completion_stream = await self._client.chat.completions.create(
                model=self._model,
                messages=_to_openai_messages(payload),
                max_tokens=payload.get("max_tokens"),
                stream=True,
                stream_options={"include_usage": True},
            )
        except openai.APIStatusError as exc:
            raise UpstreamError(exc.status_code, str(exc)) from exc
        except openai.APIConnectionError as exc:
            raise UpstreamError(502, str(exc)) from exc

        message_id = f"msg_{uuid.uuid4().hex}"
        yield _sse(
            "message_start",
            {
                "type": "message_start",
                "message": {
                    "id": message_id,
                    "model": self._model,
                    "usage": {"input_tokens": 0, "output_tokens": 0},
                },
            },
        )
        yield _sse(
            "content_block_start",
            {"type": "content_block_start", "index": 0, "content_block": {"type": "text", "text": ""}},
        )

        stop_reason = "end_turn"
        async for chunk in completion_stream:
            if chunk.choices:
                choice = chunk.choices[0]
                if choice.delta and choice.delta.content:
                    yield _sse(
                        "content_block_delta",
                        {
                            "type": "content_block_delta",
                            "index": 0,
                            "delta": {"type": "text_delta", "text": choice.delta.content},
                        },
                    )
                if choice.finish_reason == "length":
                    stop_reason = "max_tokens"
            if chunk.usage:
                usage.input_tokens = chunk.usage.prompt_tokens
                usage.output_tokens = chunk.usage.completion_tokens

        yield _sse("content_block_stop", {"type": "content_block_stop", "index": 0})
        yield _sse(
            "message_delta",
            {
                "type": "message_delta",
                "delta": {"stop_reason": stop_reason},
                "usage": {"output_tokens": usage.output_tokens},
            },
        )
        yield _sse("message_stop", {"type": "message_stop"})
