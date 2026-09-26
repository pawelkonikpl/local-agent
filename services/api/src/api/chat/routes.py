import uuid

import httpx
from fastapi import APIRouter, Depends, HTTPException, Request, Response, status
from fastapi.responses import StreamingResponse
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from api.chat.history import build_llm_messages
from api.chat.schemas import CreateMessageRequest, MessageOut, ModelsOut, SessionOut, SessionUsageOut
from api.chat.streaming import SessionTaskManager, run_generation, stream_queue
from api.config import settings
from api.db.session import get_db
from api.deps import get_current_user
from api.db.models.user import User
from api.tools.registry import ToolRegistry
from shared.db.models import ChatSession, Message, TokenUsage

router = APIRouter(prefix="/sessions", tags=["chat"])
models_router = APIRouter(prefix="/models", tags=["chat"])


async def get_llm_proxy_client(request: Request) -> httpx.AsyncClient:
    return request.app.state.llm_proxy_client


async def get_session_task_manager(request: Request) -> SessionTaskManager:
    return request.app.state.session_task_manager


async def get_tool_registry(request: Request) -> ToolRegistry:
    return request.app.state.tool_registry


async def _get_owned_session(session_id: uuid.UUID, user: User, db: AsyncSession) -> ChatSession:
    session = await db.get(ChatSession, session_id)
    if session is None or session.user_id != user.id:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Session not found")
    return session


@models_router.get("", response_model=ModelsOut)
async def list_models(user: User = Depends(get_current_user)) -> ModelsOut:
    return ModelsOut(models=settings.available_chat_models, default=settings.chat_model)


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
    client: httpx.AsyncClient = Depends(get_llm_proxy_client),
    manager: SessionTaskManager = Depends(get_session_task_manager),
    tools: ToolRegistry = Depends(get_tool_registry),
) -> StreamingResponse:
    session = await _get_owned_session(session_id, user, db)
    model = payload.model or settings.chat_model
    if model not in settings.available_chat_models:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, f"Unknown model: {model}")

    last_seq_result = await db.execute(
        select(func.max(Message.sequence_number)).where(Message.session_id == session.id)
    )
    last_seq: int = last_seq_result.scalar() or 0

    history_result = await db.execute(
        select(Message).where(Message.session_id == session.id).order_by(Message.sequence_number)
    )
    user_message = Message(
        session_id=session.id,
        sequence_number=last_seq + 1,
        role="user",
        content=[{"type": "text", "text": payload.content}],
    )
    # The new message goes through `build_llm_messages` with the rest, so it's merged with (and
    # placed after) any trailing `tool_result`s the previous generation left behind.
    anthropic_messages = build_llm_messages([*history_result.scalars(), user_message])

    # Raises 409 if a generation is already in progress -- do this before persisting the user
    # message, so a rejected second POST doesn't consume a sequence_number or get saved.
    queue = manager.start(
        session.id,
        run_generation(
            manager,
            session_id=session.id,
            user_id=user.id,
            client=client,
            tools=tools,
            model=model,
            first_sequence_number=last_seq + 2,
            anthropic_messages=anthropic_messages,
        ),
    )

    db.add(user_message)
    await db.commit()

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
