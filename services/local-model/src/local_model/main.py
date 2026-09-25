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


def _sse(data: dict) -> bytes:
    return f"data: {json.dumps(data)}\n\n".encode()


def _require_bearer_auth(request: Request) -> None:
    """OpenAI-client convention: a bearer API key, no separate session/user headers -- this
    service is only ever reached through llm-proxy's OpenAIBackend now, which owns per-user
    budget/usage tracking centrally, so local-model itself stays stateless."""
    scheme, _, token = request.headers.get("authorization", "").partition(" ")
    if scheme.lower() != "bearer" or not secrets.compare_digest(token, settings.internal_proxy_token):
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "Invalid or missing API key")


def _extract_text(content: str | list[dict]) -> str:
    if isinstance(content, str):
        return content
    return "".join(block.get("text", "") for block in content if block.get("type") == "text")


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

        messages = [
            {"role": m["role"], "content": _extract_text(m["content"])}
            for m in payload.get("messages", [])
            if m.get("role") in ("user", "assistant", "system")
        ]
        max_new_tokens = min(
            payload.get("max_tokens") or settings.max_new_tokens_cap, settings.max_new_tokens_cap
        )
        prompt_tokens = model_module.count_prompt_tokens(messages)
        completion_id = f"chatcmpl-{uuid.uuid4().hex}"
        created = int(time.time())

        async def event_stream() -> AsyncIterator[bytes]:
            generated_chunks: list[str] = []
            for chunk in model_module.generate_stream(messages, max_new_tokens):
                if not chunk:
                    continue
                generated_chunks.append(chunk)
                yield _sse(
                    {
                        "id": completion_id,
                        "object": "chat.completion.chunk",
                        "created": created,
                        "model": settings.model_id,
                        "choices": [{"index": 0, "delta": {"content": chunk}, "finish_reason": None}],
                    }
                )

            completion_tokens = model_module.count_generated_tokens("".join(generated_chunks))
            yield _sse(
                {
                    "id": completion_id,
                    "object": "chat.completion.chunk",
                    "created": created,
                    "model": settings.model_id,
                    "choices": [{"index": 0, "delta": {}, "finish_reason": "stop"}],
                    "usage": {
                        "prompt_tokens": prompt_tokens,
                        "completion_tokens": completion_tokens,
                        "total_tokens": prompt_tokens + completion_tokens,
                    },
                }
            )
            yield b"data: [DONE]\n\n"

        return StreamingResponse(event_stream(), media_type="text/event-stream")

    return app


app = create_app()


if __name__ == "__main__":
    import uvicorn

    uvicorn.run("local_model.main:app", host="0.0.0.0", port=8080)
