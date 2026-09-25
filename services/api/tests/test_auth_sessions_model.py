from datetime import datetime, timedelta, timezone

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from api.auth.security import hash_password, hash_token
from api.db.models.auth_session import AuthSession
from api.db.models.user import User


async def _make_user(db_session: AsyncSession) -> User:
    user = User(email="alice@example.com", password_hash=hash_password("irrelevant"), role="user")
    db_session.add(user)
    await db_session.flush()
    return user


async def test_auth_session_created_with_token_hash_only(db_session: AsyncSession) -> None:
    user = await _make_user(db_session)
    raw_token = "raw-token-value"
    auth_session = AuthSession(
        user_id=user.id,
        token_hash=hash_token(raw_token),
        expires_at=datetime.now(timezone.utc) + timedelta(hours=1),
    )
    db_session.add(auth_session)
    await db_session.flush()

    result = await db_session.execute(select(AuthSession).where(AuthSession.user_id == user.id))
    stored = result.scalar_one()
    assert stored.token_hash != raw_token
    assert stored.token_hash == hash_token(raw_token)
    assert stored.revoked_at is None


async def test_auth_session_expiry_is_respected(db_session: AsyncSession) -> None:
    user = await _make_user(db_session)
    expired = AuthSession(
        user_id=user.id,
        token_hash=hash_token("expired-token"),
        expires_at=datetime.now(timezone.utc) - timedelta(seconds=1),
    )
    db_session.add(expired)
    await db_session.flush()

    assert expired.expires_at < datetime.now(timezone.utc)


async def test_auth_session_revocation(db_session: AsyncSession) -> None:
    user = await _make_user(db_session)
    auth_session = AuthSession(
        user_id=user.id,
        token_hash=hash_token("some-token"),
        expires_at=datetime.now(timezone.utc) + timedelta(hours=1),
    )
    db_session.add(auth_session)
    await db_session.flush()

    assert auth_session.revoked_at is None
    auth_session.revoked_at = datetime.now(timezone.utc)
    await db_session.flush()

    result = await db_session.execute(select(AuthSession).where(AuthSession.id == auth_session.id))
    stored = result.scalar_one()
    assert stored.revoked_at is not None
