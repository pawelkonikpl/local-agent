import uuid
from collections.abc import AsyncGenerator, AsyncIterator
from contextlib import asynccontextmanager

import httpx
import openai
from contracts.auth import is_valid_bearer
from contracts.llm_proxy import (
    MESSAGES_PATH,
    MODELS_PATH,
    SESSION_ID_HEADER,
    USER_ID_HEADER,
    ModelsResponse,
)
from fastapi import Depends, FastAPI, HTTPException, Request, status
from fastapi.responses import StreamingResponse
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from llm_proxy import metering
from llm_proxy.backends.anthropic_backend import (
    AnthropicBackend,
    build_anthropic_client,
)
from llm_proxy.backends.base import (
    BackendRouter,
    LLMBackend,
    NoBackendError,
    UpstreamError,
)
from llm_proxy.backends.openai_responses_backend import OpenAIResponsesBackend
from llm_proxy.config import settings
from llm_proxy.db import engine, get_db
from llm_proxy.model_catalog import ModelCatalog
from llm_proxy.usage import UsageAccumulator


async def get_anthropic_client(request: Request) -> httpx.AsyncClient:
    return request.app.state.anthropic_client


async def get_openai_client(request: Request) -> openai.AsyncOpenAI | None:
    return request.app.state.openai_client


async def get_model_catalog(request: Request) -> ModelCatalog:
    return request.app.state.model_catalog


def build_router(anthropic_client: httpx.AsyncClient, openai_client: openai.AsyncOpenAI | None) -> BackendRouter:
    """Every configured provider. OpenAI only with a client: it's opt-in (`OPENAI_API_KEY`)."""
    backends: list[LLMBackend] = [AnthropicBackend(anthropic_client, settings.anthropic_api_key)]
    if openai_client is not None:
        backends.append(
            OpenAIResponsesBackend(
                openai_client,
                model_pattern=settings.openai_models_pattern,
                reasoning_efforts=settings.openai_reasoning_efforts,
            )
        )
    return BackendRouter(backends)


async def get_backend_router(
    anthropic_client: httpx.AsyncClient = Depends(get_anthropic_client),
    openai_client: openai.AsyncOpenAI | None = Depends(get_openai_client),
) -> BackendRouter:
    return build_router(anthropic_client, openai_client)


def require_internal_auth(request: Request) -> None:
    if not is_valid_bearer(request.headers.get("authorization"), settings.internal_proxy_token):
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "Invalid or missing internal proxy token")


def _require_uuid_header(request: Request, name: str) -> uuid.UUID:
    raw = request.headers.get(name)
    if raw is None:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, f"Missing {name} header")
    try:
        return uuid.UUID(raw)
    except ValueError as exc:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, f"Invalid {name} header") from exc


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncGenerator[None, None]:
    async with engine.connect() as conn:
        await conn.execute(text("SELECT 1"))
    app.state.anthropic_client = build_anthropic_client(settings.anthropic_base_url)
    # Constructing openai.AsyncOpenAI with no api_key raises, so an unconfigured provider is never
    # instantiated; `build_router` leaves it out instead.
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


def create_app() -> FastAPI:
    app = FastAPI(title="local-agent llm-proxy", lifespan=lifespan, dependencies=[Depends(require_internal_auth)])

    @app.get(MODELS_PATH, response_model=ModelsResponse)
    async def list_models(
        catalog: ModelCatalog = Depends(get_model_catalog),
        router: BackendRouter = Depends(get_backend_router),
    ) -> ModelsResponse:
        """What every configured provider serves right now -- api's model picker and its check
        of a requested model both read this, so neither side keeps its own list."""
        models = await catalog.models(router.backends)
        if not models:
            raise HTTPException(status.HTTP_502_BAD_GATEWAY, "No provider could list its models")
        return ModelsResponse(models=models)

    @app.post(MESSAGES_PATH)
    async def create_message(
        request: Request,
        db: AsyncSession = Depends(get_db),
        router: BackendRouter = Depends(get_backend_router),
    ) -> StreamingResponse:
        user_id = _require_uuid_header(request, USER_ID_HEADER)
        session_id = _require_uuid_header(request, SESSION_ID_HEADER)
        payload = await request.json()
        model = payload.get("model", "")

        try:
            await metering.ensure_within_budget(db, user_id)
        except metering.BudgetExceededError as exc:
            raise HTTPException(status.HTTP_402_PAYMENT_REQUIRED, str(exc)) from exc
        try:
            backend = router.for_model(model)
        except NoBackendError as exc:
            raise HTTPException(status.HTTP_503_SERVICE_UNAVAILABLE, str(exc)) from exc

        usage = UsageAccumulator(fallback_model=model or "unknown")
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
