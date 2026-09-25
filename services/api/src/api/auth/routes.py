from datetime import datetime, timedelta, timezone

from fastapi import APIRouter, Depends, HTTPException, Request, Response, status
from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from api.auth.security import generate_session_token, hash_token, verify_password
from api.config import settings
from api.db.models.auth_session import AuthSession
from api.db.models.user import User
from api.db.session import get_db
from api.deps import get_current_user
from api.schemas import UserOut

router = APIRouter(prefix="/auth", tags=["auth"])


class LoginRequest(BaseModel):
    email: str
    password: str


@router.post("/login", response_model=UserOut)
async def login(
    payload: LoginRequest, response: Response, db: AsyncSession = Depends(get_db)
) -> User:
    result = await db.execute(select(User).where(User.email == payload.email))
    user = result.scalar_one_or_none()
    if user is None:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "Invalid email or password")

    valid, rehash = verify_password(user.password_hash, payload.password)
    if not valid:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "Invalid email or password")
    if rehash is not None:
        user.password_hash = rehash

    token = generate_session_token()
    expires_at = datetime.now(timezone.utc) + timedelta(hours=settings.session_ttl_hours)
    db.add(AuthSession(user_id=user.id, token_hash=hash_token(token), expires_at=expires_at))
    await db.commit()

    response.set_cookie(
        settings.session_cookie_name,
        token,
        httponly=True,
        secure=settings.cookie_secure,
        samesite="lax",
        max_age=settings.session_ttl_hours * 3600,
    )
    return user


@router.post("/logout", status_code=status.HTTP_204_NO_CONTENT)
async def logout(request: Request, response: Response, db: AsyncSession = Depends(get_db)) -> None:
    token = request.cookies.get(settings.session_cookie_name)
    if token is not None:
        result = await db.execute(
            select(AuthSession).where(AuthSession.token_hash == hash_token(token))
        )
        auth_session = result.scalar_one_or_none()
        if auth_session is not None and auth_session.revoked_at is None:
            auth_session.revoked_at = datetime.now(timezone.utc)
            await db.commit()
    response.delete_cookie(settings.session_cookie_name)


@router.get("/me", response_model=UserOut)
async def me(user: User = Depends(get_current_user)) -> User:
    return user
