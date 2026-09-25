import uuid

import httpx
from fastapi import APIRouter, Depends, HTTPException, Request, status
from fastapi.responses import StreamingResponse
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from api.chat.schemas import CreateMessageRequest, MessageOut, SessionOut
from api.chat.streaming import stream_chat_response
from api.db.session import get_db
from api.deps import get_current_user
from api.db.models.user import User
from shared.db.models import ChatSession, Message

router = APIRouter(prefix="/sessions", tags=["chat"])


async def get_llm_proxy_client(request: Request) -> httpx.AsyncClient:
    return request.app.state.llm_proxy_client


async def _get_owned_session(session_id: uuid.UUID, user: User, db: AsyncSession) -> ChatSession:
    session = await db.get(ChatSession, session_id)
    if session is None or session.user_id != user.id:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Session not found")
    return session


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
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
) -> list[Message]:
    await _get_owned_session(session_id, user, db)
    result = await db.execute(
        select(Message).where(Message.session_id == session_id).order_by(Message.sequence_number)
    )
    return list(result.scalars())


@router.post("/{session_id}/messages")
async def post_message(
    session_id: uuid.UUID,
    payload: CreateMessageRequest,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
    client: httpx.AsyncClient = Depends(get_llm_proxy_client),
) -> StreamingResponse:
    session = await _get_owned_session(session_id, user, db)

    last_seq_result = await db.execute(
        select(func.max(Message.sequence_number)).where(Message.session_id == session.id)
    )
    last_seq: int = last_seq_result.scalar() or 0

    history_result = await db.execute(
        select(Message).where(Message.session_id == session.id).order_by(Message.sequence_number)
    )
    anthropic_messages = [
        {"role": m.role, "content": m.content}
        for m in history_result.scalars()
        if m.role in ("user", "assistant")
    ]
    anthropic_messages.append({"role": "user", "content": [{"type": "text", "text": payload.content}]})

    db.add(
        Message(
            session_id=session.id,
            sequence_number=last_seq + 1,
            role="user",
            content=[{"type": "text", "text": payload.content}],
        )
    )
    await db.commit()

    generator = stream_chat_response(
        client,
        db,
        session_id=session.id,
        user_id=user.id,
        assistant_sequence_number=last_seq + 2,
        anthropic_messages=anthropic_messages,
    )
    return StreamingResponse(generator, media_type="text/event-stream")
