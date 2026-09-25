from collections.abc import Callable, Coroutine
from datetime import datetime, timezone

from fastapi import Depends, HTTPException, Request, status
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from api.auth.security import hash_token
from api.config import settings
from api.db.models.auth_session import AuthSession
from api.db.models.user import User
from api.db.session import get_db


async def get_current_user(request: Request, db: AsyncSession = Depends(get_db)) -> User:
    token = request.cookies.get(settings.session_cookie_name)
    if token is None:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "Not authenticated")

    token_hash = hash_token(token)
    result = await db.execute(select(AuthSession).where(AuthSession.token_hash == token_hash))
    auth_session = result.scalar_one_or_none()
    if (
        auth_session is None
        or auth_session.revoked_at is not None
        or auth_session.expires_at < datetime.now(timezone.utc)
    ):
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "Not authenticated")

    user = await db.get(User, auth_session.user_id)
    if user is None:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "Not authenticated")
    return user


def require_role(role: str) -> Callable[..., Coroutine[None, None, User]]:
    async def _check(user: User = Depends(get_current_user)) -> User:
        if user.role != role:
            raise HTTPException(status.HTTP_403_FORBIDDEN, "Forbidden")
        return user

    return _check
