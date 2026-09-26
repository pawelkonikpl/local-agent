from collections.abc import AsyncIterator

import openai

from llm_proxy.backends.base import UpstreamError
from llm_proxy.backends.openai_translate import StreamTranslator, to_openai_messages, to_openai_tool_kwargs
from llm_proxy.usage import UsageAccumulator


class OpenAIBackend:
    """Any provider speaking OpenAI's Chat Completions format natively -- real OpenAI, or a
    self-hosted model exposing an OpenAI-compatible endpoint (this project's own `local-model`
    service included). Translates the Anthropic request into Chat Completions (tools included,
    via OpenAI's native `tools` / `tool_calls`) and the streamed chunks back into Anthropic
    Messages SSE events, since that's the one contract session-agent's `anthropic` SDK
    understands. See `openai_translate` for both directions.
    """

    def __init__(self, client: openai.AsyncOpenAI, model: str) -> None:
        self._client = client
        self._model = model

    async def stream(self, payload: dict, usage: UsageAccumulator) -> AsyncIterator[bytes]:
        usage.model = self._model
        try:
            completion_stream = await self._client.chat.completions.create(
                model=self._model,
                messages=to_openai_messages(payload),
                max_tokens=payload.get("max_tokens"),
                stream=True,
                stream_options={"include_usage": True},
                **to_openai_tool_kwargs(payload),
            )
        except openai.APIStatusError as exc:
            raise UpstreamError(exc.status_code, str(exc)) from exc
        except openai.APIConnectionError as exc:
            raise UpstreamError(502, str(exc)) from exc

        translator = StreamTranslator(self._model, usage)
        for event in translator.start():
            yield event
        async for chunk in completion_stream:
            for event in translator.on_chunk(chunk):
                yield event
        for event in translator.finish():
            yield event
