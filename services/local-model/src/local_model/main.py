import json
import secrets
import time
import uuid
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from fastapi import FastAPI, HTTPException, Request, status
from fastapi.responses import StreamingResponse

from local_model import model as model_module
from local_model.config import settings
from local_model.prompt import to_template_messages, to_template_tools
from local_model.tool_calls import ChatCompletionDeltas


def _sse(data: dict) -> bytes:
    return f"data: {json.dumps(data)}\n\n".encode()


def _require_bearer_auth(request: Request) -> None:
    """OpenAI-client convention: a bearer API key, no separate session/user headers -- this
    service is only ever reached through llm-proxy's OpenAIBackend now, which owns per-user
    budget/usage tracking centrally, so local-model itself stays stateless."""
    scheme, _, token = request.headers.get("authorization", "").partition(" ")
    if scheme.lower() != "bearer" or not secrets.compare_digest(token, settings.internal_proxy_token):
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "Invalid or missing API key")


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    model_module.load()
    yield


def create_app() -> FastAPI:
    app = FastAPI(title="local-agent local-model", lifespan=lifespan)

    @app.get("/health")
    async def health() -> dict:
        return {"status": "ok"}

    @app.post("/v1/chat/completions")
    async def create_chat_completion(request: Request) -> StreamingResponse:
        """OpenAI Chat Completions-shaped subset (streaming only) -- the upstream llm-proxy's
        `OpenAIBackend` talks to when a request's `model` is `"local-model"`. Not a general
        OpenAI-compatible server: just enough surface for that one caller."""
        _require_bearer_auth(request)
        payload = await request.json()

        messages = to_template_messages(payload.get("messages", []))
        tools = to_template_tools(payload)
        max_new_tokens = min(
            payload.get("max_tokens") or settings.max_new_tokens_cap, settings.max_new_tokens_cap
        )
        prompt_tokens = model_module.count_prompt_tokens(messages, tools)
        completion_id = f"chatcmpl-{uuid.uuid4().hex}"
        created = int(time.time())

        def chunk(delta: dict, finish_reason: str | None = None, usage: dict | None = None) -> bytes:
            data = {
                "id": completion_id,
                "object": "chat.completion.chunk",
                "created": created,
                "model": settings.model_id,
                "choices": [{"index": 0, "delta": delta, "finish_reason": finish_reason}],
            }
            if usage is not None:
                data["usage"] = usage
            return _sse(data)

        async def event_stream() -> AsyncIterator[bytes]:
            generated_chunks: list[str] = []
            deltas = ChatCompletionDeltas()

            for text in model_module.generate_stream(messages, max_new_tokens, tools):
                if not text:
                    continue
                generated_chunks.append(text)
                for delta in deltas.feed(text):
                    yield chunk(delta)
            for delta in deltas.finish():
                yield chunk(delta)

            completion_tokens = model_module.count_generated_tokens("".join(generated_chunks))
            yield chunk(
                {},
                deltas.finish_reason,
                usage={
                    "prompt_tokens": prompt_tokens,
                    "completion_tokens": completion_tokens,
                    "total_tokens": prompt_tokens + completion_tokens,
                },
            )
            yield b"data: [DONE]\n\n"

        return StreamingResponse(event_stream(), media_type="text/event-stream")

    return app


app = create_app()


if __name__ == "__main__":
    import uvicorn

    uvicorn.run("local_model.main:app", host="0.0.0.0", port=8080)
