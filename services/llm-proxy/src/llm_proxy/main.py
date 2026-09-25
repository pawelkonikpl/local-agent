import secrets
import uuid
from collections.abc import AsyncGenerator, AsyncIterator
from contextlib import asynccontextmanager

import httpx
import openai
from fastapi import Depends, FastAPI, HTTPException, Request, status
from fastapi.responses import StreamingResponse
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from llm_proxy import metering
from llm_proxy.backends.anthropic_backend import AnthropicBackend, build_anthropic_client
from llm_proxy.backends.base import LLMBackend, UpstreamError
from llm_proxy.backends.openai_backend import OpenAIBackend
from llm_proxy.config import settings
from llm_proxy.db import engine, get_db
from llm_proxy.usage import UsageAccumulator

LOCAL_MODEL_ALIAS = "local-model"


async def get_anthropic_client(request: Request) -> httpx.AsyncClient:
    return request.app.state.anthropic_client


async def get_openai_client(request: Request) -> openai.AsyncOpenAI | None:
    return request.app.state.openai_client


async def get_local_model_client(request: Request) -> openai.AsyncOpenAI | None:
    return request.app.state.local_model_client


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncGenerator[None, None]:
    async with engine.connect() as conn:
        await conn.execute(text("SELECT 1"))
    app.state.anthropic_client = build_anthropic_client(settings.anthropic_base_url)
    # Both optional providers stay None unless explicitly configured -- constructing
    # openai.AsyncOpenAI with no api_key raises at startup, so an unconfigured provider must
    # never be instantiated just because the dependency is always wired into the route.
    app.state.openai_client = (
        openai.AsyncOpenAI(api_key=settings.openai_api_key, base_url=settings.openai_base_url)
        if settings.openai_api_key
        else None
    )
    app.state.local_model_client = (
        openai.AsyncOpenAI(api_key=settings.internal_proxy_token, base_url=settings.local_model_base_url)
        if settings.local_model_base_url
        else None
    )
    yield
    await app.state.anthropic_client.aclose()
    if app.state.openai_client is not None:
        await app.state.openai_client.close()
    if app.state.local_model_client is not None:
        await app.state.local_model_client.close()


def _require_internal_auth(request: Request) -> None:
    scheme, _, token = request.headers.get("authorization", "").partition(" ")
    if scheme.lower() != "bearer" or not secrets.compare_digest(token, settings.internal_proxy_token):
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "Invalid or missing internal proxy token")


def _require_uuid_header(request: Request, name: str) -> uuid.UUID:
    raw = request.headers.get(name)
    if raw is None:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, f"Missing {name} header")
    try:
        return uuid.UUID(raw)
    except ValueError as exc:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, f"Invalid {name} header") from exc


def _resolve_backend(
    model: str,
    *,
    anthropic_client: httpx.AsyncClient,
    openai_client: openai.AsyncOpenAI | None,
    local_model_client: openai.AsyncOpenAI | None,
) -> LLMBackend:
    if model.startswith("claude-"):
        return AnthropicBackend(anthropic_client, settings.anthropic_api_key)
    if model == LOCAL_MODEL_ALIAS:
        if local_model_client is None:
            raise HTTPException(status.HTTP_503_SERVICE_UNAVAILABLE, "local-model backend is not configured")
        return OpenAIBackend(local_model_client, settings.local_model_id)
    if openai_client is None:
        raise HTTPException(status.HTTP_503_SERVICE_UNAVAILABLE, "OpenAI backend is not configured")
    return OpenAIBackend(openai_client, model)


def create_app() -> FastAPI:
    app = FastAPI(title="local-agent llm-proxy", lifespan=lifespan)

    @app.post("/v1/messages")
    async def create_message(
        request: Request,
        db: AsyncSession = Depends(get_db),
        anthropic_client: httpx.AsyncClient = Depends(get_anthropic_client),
        openai_client: openai.AsyncOpenAI | None = Depends(get_openai_client),
        local_model_client: openai.AsyncOpenAI | None = Depends(get_local_model_client),
    ) -> StreamingResponse:
        _require_internal_auth(request)
        user_id = _require_uuid_header(request, "x-local-agent-user-id")
        session_id = _require_uuid_header(request, "x-local-agent-session-id")
        payload = await request.json()

        try:
            await metering.ensure_within_budget(db, user_id)
        except metering.BudgetExceededError as exc:
            raise HTTPException(status.HTTP_402_PAYMENT_REQUIRED, str(exc)) from exc

        backend = _resolve_backend(
            payload.get("model", ""),
            anthropic_client=anthropic_client,
            openai_client=openai_client,
            local_model_client=local_model_client,
        )
        usage = UsageAccumulator(fallback_model=payload.get("model", "unknown"))
        upstream = backend.stream(payload, usage)
        try:
            first_chunk = await anext(upstream, None)
        except UpstreamError as exc:
            raise HTTPException(exc.status_code, str(exc)) from exc

        async def event_stream() -> AsyncIterator[bytes]:
            try:
                if first_chunk is not None:
                    yield first_chunk
                async for chunk in upstream:
                    yield chunk
            finally:
                if usage.input_tokens or usage.output_tokens:
                    await metering.record_usage(
                        db,
                        session_id=session_id,
                        user_id=user_id,
                        model=usage.model,
                        input_tokens=usage.input_tokens,
                        output_tokens=usage.output_tokens,
                    )

        return StreamingResponse(event_stream(), media_type="text/event-stream")

    return app


app = create_app()


if __name__ == "__main__":
    import uvicorn

    uvicorn.run("llm_proxy.main:app", host="0.0.0.0", port=8080, reload=True)
