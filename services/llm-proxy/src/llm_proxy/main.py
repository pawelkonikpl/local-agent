import secrets
import uuid
from collections.abc import AsyncGenerator, AsyncIterator
from contextlib import asynccontextmanager

import httpx
import openai
from fastapi import Depends, FastAPI, HTTPException, Request, status
from fastapi.responses import StreamingResponse
from pydantic import BaseModel
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from llm_proxy import metering
from llm_proxy.backends.anthropic_backend import AnthropicBackend, build_anthropic_client
from llm_proxy.backends.base import LLMBackend, ModelInfo, UpstreamError
from llm_proxy.backends.openai_responses_backend import OpenAIResponsesBackend
from llm_proxy.config import settings
from llm_proxy.db import engine, get_db
from llm_proxy.model_catalog import ModelCatalog
from llm_proxy.usage import UsageAccumulator


class ModelsResponse(BaseModel):
    models: list[ModelInfo]


async def get_anthropic_client(request: Request) -> httpx.AsyncClient:
    return request.app.state.anthropic_client


async def get_openai_client(request: Request) -> openai.AsyncOpenAI | None:
    return request.app.state.openai_client


async def get_model_catalog(request: Request) -> ModelCatalog:
    return request.app.state.model_catalog


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncGenerator[None, None]:
    async with engine.connect() as conn:
        await conn.execute(text("SELECT 1"))
    app.state.anthropic_client = build_anthropic_client(settings.anthropic_base_url)
    # OpenAI stays None unless explicitly configured -- constructing openai.AsyncOpenAI with no
    # api_key raises at startup, so an unconfigured provider must never be instantiated just
    # because the dependency is always wired into the route.
    app.state.openai_client = (
        openai.AsyncOpenAI(api_key=settings.openai_api_key, base_url=settings.openai_base_url)
        if settings.openai_api_key
        else None
    )
    app.state.model_catalog = ModelCatalog(settings.models_cache_ttl_s)
    yield
    await app.state.anthropic_client.aclose()
    if app.state.openai_client is not None:
        await app.state.openai_client.close()


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


def _openai_backend(client: openai.AsyncOpenAI) -> OpenAIResponsesBackend:
    return OpenAIResponsesBackend(
        client,
        model_pattern=settings.openai_models_pattern,
        reasoning_efforts=settings.openai_reasoning_efforts,
    )


def _configured_backends(
    anthropic_client: httpx.AsyncClient, openai_client: openai.AsyncOpenAI | None
) -> list[LLMBackend]:
    backends: list[LLMBackend] = [AnthropicBackend(anthropic_client, settings.anthropic_api_key)]
    if openai_client is not None:
        backends.append(_openai_backend(openai_client))
    return backends


def _resolve_backend(
    model: str,
    *,
    anthropic_client: httpx.AsyncClient,
    openai_client: openai.AsyncOpenAI | None,
) -> LLMBackend:
    if model.startswith("claude-"):
        return AnthropicBackend(anthropic_client, settings.anthropic_api_key)
    if openai_client is None:
        raise HTTPException(status.HTTP_503_SERVICE_UNAVAILABLE, "OpenAI backend is not configured")
    return _openai_backend(openai_client)


def create_app() -> FastAPI:
    app = FastAPI(title="local-agent llm-proxy", lifespan=lifespan)

    @app.get("/v1/models", response_model=ModelsResponse)
    async def list_models(
        request: Request,
        catalog: ModelCatalog = Depends(get_model_catalog),
        anthropic_client: httpx.AsyncClient = Depends(get_anthropic_client),
        openai_client: openai.AsyncOpenAI | None = Depends(get_openai_client),
    ) -> ModelsResponse:
        """What every configured provider serves right now -- api's model picker and its check
        of a requested model both read this, so neither side keeps its own list."""
        _require_internal_auth(request)
        models = await catalog.models(_configured_backends(anthropic_client, openai_client))
        if not models:
            raise HTTPException(status.HTTP_502_BAD_GATEWAY, "No provider could list its models")
        return ModelsResponse(models=models)

    @app.post("/v1/messages")
    async def create_message(
        request: Request,
        db: AsyncSession = Depends(get_db),
        anthropic_client: httpx.AsyncClient = Depends(get_anthropic_client),
        openai_client: openai.AsyncOpenAI | None = Depends(get_openai_client),
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
