import re
from collections.abc import AsyncIterator

import openai

from llm_proxy.backends.base import ModelInfo, UpstreamError
from llm_proxy.backends.openai_responses_translate import (
    ResponsesStreamTranslator,
    to_responses_input,
    to_responses_kwargs,
)
from llm_proxy.usage import UsageAccumulator


class OpenAIResponsesBackend:
    """Real OpenAI, through the Responses API (`/v1/responses`) -- the only OpenAI endpoint
    that serves function tools to reasoning models with reasoning left on (Chat Completions
    rejects that combination). Translates the Anthropic request into Responses `input` items
    and the streamed events back into Anthropic Messages SSE events. See
    `openai_responses_translate` for both directions.

    Stateless: `store=False`, and every request replays the full conversation as input. The
    model's reasoning is therefore not carried over between requests -- Anthropic-shaped
    history has no slot for OpenAI's reasoning items.

    OpenAI's `/v1/models` lists every model the key can reach (embeddings, TTS, image, dated
    snapshots...) and says nothing about what each one supports, so `list_models` keeps only
    the ids matching `model_pattern` and gives each the same `reasoning_efforts`.
    """

    def __init__(
        self, client: openai.AsyncOpenAI, *, model_pattern: str, reasoning_efforts: list[str]
    ) -> None:
        self._client = client
        self._model_pattern = re.compile(model_pattern)
        self._reasoning_efforts = reasoning_efforts

    async def list_models(self) -> list[ModelInfo]:
        try:
            listed = [model async for model in self._client.models.list()]
        except openai.APIStatusError as exc:
            raise UpstreamError(exc.status_code, str(exc)) from exc
        except openai.APIConnectionError as exc:
            raise UpstreamError(502, str(exc)) from exc
        return [
            ModelInfo(id=model.id, reasoning_efforts=self._reasoning_efforts)
            for model in sorted(listed, key=lambda model: model.created, reverse=True)
            if self._model_pattern.fullmatch(model.id)
        ]

    async def stream(self, payload: dict, usage: UsageAccumulator) -> AsyncIterator[bytes]:
        model = payload.get("model", "")
        usage.model = model
        try:
            event_stream = await self._client.responses.create(
                model=model,
                input=to_responses_input(payload),
                # Also counts the model's hidden reasoning tokens, not just the visible answer.
                max_output_tokens=payload.get("max_tokens"),
                store=False,
                stream=True,
                **to_responses_kwargs(payload),
            )
        except openai.APIStatusError as exc:
            raise UpstreamError(exc.status_code, str(exc)) from exc
        except openai.APIConnectionError as exc:
            raise UpstreamError(502, str(exc)) from exc

        translator = ResponsesStreamTranslator(model, usage)
        for event in translator.start():
            yield event
        async for response_event in event_stream:
            for event in translator.on_event(response_event):
                yield event
        for event in translator.finish():
            yield event
