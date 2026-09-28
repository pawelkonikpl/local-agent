import uuid

import httpx
from fastapi import APIRouter, Depends, HTTPException, Request, Response, status
from fastapi.responses import StreamingResponse
from shared.db.models import ChatSession, Message, TokenUsage
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from api.chat.catalog import fetch_models
from api.chat.history import build_llm_messages
from api.chat.llm_proxy import LlmProxy, ModelCatalogUnavailableError
from api.chat.schemas import (
    CreateMessageRequest,
    MessageOut,
    ModelsOut,
    SessionOut,
    SessionUsageOut,
)
from api.chat.store import GenerationStore
from api.chat.streaming import ChatGeneration, SessionTaskManager, stream_queue
from api.config import settings
from api.db.models.user import User
from api.db.session import get_db, get_session_factory
from api.deps import get_current_user
from api.tools.registry import ToolRegistry

router = APIRouter(prefix="/sessions", tags=["chat"])
models_router = APIRouter(prefix="/models", tags=["chat"])


async def get_llm_proxy_client(request: Request) -> httpx.AsyncClient:
    return request.app.state.llm_proxy_client


async def get_llm_proxy(client: httpx.AsyncClient = Depends(get_llm_proxy_client)) -> LlmProxy:
    return LlmProxy(client, settings.internal_proxy_token)


async def get_generation_store(
    session_factory: async_sessionmaker[AsyncSession] = Depends(get_session_factory),
) -> GenerationStore:
    return GenerationStore(session_factory)


async def get_session_task_manager(request: Request) -> SessionTaskManager:
    return request.app.state.session_task_manager


async def get_tool_registry(request: Request) -> ToolRegistry:
    return request.app.state.tool_registry


async def _get_owned_session(session_id: uuid.UUID, user: User, db: AsyncSession) -> ChatSession:
    session = await db.get(ChatSession, session_id)
    if session is None or session.user_id != user.id:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Session not found")
    return session


async def _available_models(llm: LlmProxy) -> ModelsOut:
    try:
        return await fetch_models(llm)
    except ModelCatalogUnavailableError as exc:
        raise HTTPException(status.HTTP_502_BAD_GATEWAY, str(exc)) from exc


@models_router.get("", response_model=ModelsOut)
async def list_models(user: User = Depends(get_current_user), llm: LlmProxy = Depends(get_llm_proxy)) -> ModelsOut:
    return await _available_models(llm)


@router.post("", response_model=SessionOut, status_code=status.HTTP_201_CREATED)
async def create_session(
    user: User = Depends(get_current_user), db: AsyncSession = Depends(get_db)
) -> ChatSession:
    session = ChatSession(user_id=user.id)
    db.add(session)
    await db.commit()
    await db.refresh(session)
    return session


@router.get("", response_model=list[SessionOut])
async def list_sessions(
    user: User = Depends(get_current_user), db: AsyncSession = Depends(get_db)
) -> list[ChatSession]:
    result = await db.execute(
        select(ChatSession).where(ChatSession.user_id == user.id).order_by(ChatSession.created_at.desc())
    )
    return list(result.scalars())


@router.get("/{session_id}/messages", response_model=list[MessageOut])
async def list_messages(
    session_id: uuid.UUID,
    after: int | None = None,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> list[Message]:
    await _get_owned_session(session_id, user, db)
    stmt = select(Message).where(Message.session_id == session_id)
    if after is not None:
        stmt = stmt.where(Message.sequence_number > after)
    result = await db.execute(stmt.order_by(Message.sequence_number))
    return list(result.scalars())


@router.get("/{session_id}/usage", response_model=SessionUsageOut)
async def get_session_usage(
    session_id: uuid.UUID,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> SessionUsageOut:
    await _get_owned_session(session_id, user, db)
    result = await db.execute(
        select(
            func.coalesce(func.sum(TokenUsage.input_tokens), 0),
            func.coalesce(func.sum(TokenUsage.output_tokens), 0),
        ).where(TokenUsage.session_id == session_id)
    )
    input_tokens, output_tokens = result.one()
    return SessionUsageOut(
        input_tokens=input_tokens,
        output_tokens=output_tokens,
        total_tokens=input_tokens + output_tokens,
    )


@router.post("/{session_id}/messages")
async def post_message(
    session_id: uuid.UUID,
    payload: CreateMessageRequest,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
    llm: LlmProxy = Depends(get_llm_proxy),
    store: GenerationStore = Depends(get_generation_store),
    manager: SessionTaskManager = Depends(get_session_task_manager),
    tools: ToolRegistry = Depends(get_tool_registry),
) -> StreamingResponse:
    session = await _get_owned_session(session_id, user, db)
    available = await _available_models(llm)
    model = payload.model or available.default
    offered = available.get(model)
    if offered is None:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_CONTENT, f"Unknown model: {model}")
    effort = payload.reasoning_effort
    if effort is not None and effort not in offered.reasoning_efforts:
        raise HTTPException(
            status.HTTP_422_UNPROCESSABLE_CONTENT, f"Reasoning effort {effort!r} is not supported by {model}"
        )

    history_result = await db.execute(
        select(Message).where(Message.session_id == session.id).order_by(Message.sequence_number)
    )
    history = list(history_result.scalars())
    last_seq = history[-1].sequence_number if history else 0
    user_message = Message(
        session_id=session.id,
        sequence_number=last_seq + 1,
        role="user",
        content=[{"type": "text", "text": payload.content}],
    )
    # The new message goes through `build_llm_messages` with the rest, so it's merged with (and
    # placed after) any trailing `tool_result`s the previous generation left behind.
    anthropic_messages = build_llm_messages([*history, user_message])

    generation = ChatGeneration(
        session_id=session.id,
        user_id=user.id,
        model=model,
        reasoning_effort=effort,
        manager=manager,
        llm=llm,
        store=store,
        tools=tools,
    )
    # Raises 409 if a generation is already in progress -- before persisting the user message, so
    # a rejected second POST doesn't consume a sequence_number or get saved. The generation only
    # starts once the message is committed, so no reply is ever saved ahead of its question.
    queue = manager.reserve(session.id)
    try:
        db.add(user_message)
        await db.commit()
    except BaseException:
        manager.release(session.id)
        raise
    manager.start(session.id, generation.run(anthropic_messages, first_sequence_number=last_seq + 2))

    return StreamingResponse(stream_queue(queue), media_type="text/event-stream")


@router.get("/{session_id}/stream")
async def stream_session(
    session_id: uuid.UUID,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
    manager: SessionTaskManager = Depends(get_session_task_manager),
) -> Response:
    await _get_owned_session(session_id, user, db)
    queue = manager.subscribe(session_id)
    if queue is None:
        return Response(status_code=status.HTTP_204_NO_CONTENT)
    return StreamingResponse(stream_queue(queue), media_type="text/event-stream")


@router.post("/{session_id}/cancel", status_code=status.HTTP_204_NO_CONTENT)
async def cancel_session(
    session_id: uuid.UUID,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
    manager: SessionTaskManager = Depends(get_session_task_manager),
) -> Response:
    await _get_owned_session(session_id, user, db)
    manager.cancel(session_id)
    return Response(status_code=status.HTTP_204_NO_CONTENT)
