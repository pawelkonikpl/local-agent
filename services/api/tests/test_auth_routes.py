from httpx import AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession

from api.auth.security import hash_password
from api.db.models.user import User


async def _create_user(db_session: AsyncSession, email: str, password: str, role: str = "user") -> User:
    user = User(email=email, password_hash=hash_password(password), role=role)
    db_session.add(user)
    await db_session.flush()
    return user


async def test_login_with_correct_credentials_sets_cookie_and_returns_user(
    client: AsyncClient, db_session: AsyncSession
) -> None:
    await _create_user(db_session, "carol@example.com", "carol-pass-123")

    response = await client.post(
        "/auth/login", json={"email": "carol@example.com", "password": "carol-pass-123"}
    )

    assert response.status_code == 200
    assert response.json()["email"] == "carol@example.com"
    assert "la_session" in response.cookies


async def test_login_with_wrong_password_returns_401(client: AsyncClient, db_session: AsyncSession) -> None:
    await _create_user(db_session, "dave@example.com", "dave-pass-123")

    response = await client.post(
        "/auth/login", json={"email": "dave@example.com", "password": "wrong-password"}
    )

    assert response.status_code == 401
    assert "la_session" not in response.cookies


async def test_login_with_unknown_email_returns_401(client: AsyncClient) -> None:
    response = await client.post(
        "/auth/login", json={"email": "nobody@example.com", "password": "whatever"}
    )
    assert response.status_code == 401


async def test_me_without_cookie_returns_401(client: AsyncClient) -> None:
    response = await client.get("/auth/me")
    assert response.status_code == 401


async def test_me_with_valid_cookie_returns_current_user(
    client: AsyncClient, db_session: AsyncSession
) -> None:
    await _create_user(db_session, "erin@example.com", "erin-pass-123")
    await client.post("/auth/login", json={"email": "erin@example.com", "password": "erin-pass-123"})

    response = await client.get("/auth/me")

    assert response.status_code == 200
    assert response.json()["email"] == "erin@example.com"


async def test_logout_revokes_session_so_me_then_returns_401(
    client: AsyncClient, db_session: AsyncSession
) -> None:
    await _create_user(db_session, "frank@example.com", "frank-pass-123")
    await client.post("/auth/login", json={"email": "frank@example.com", "password": "frank-pass-123"})

    logout_response = await client.post("/auth/logout")
    assert logout_response.status_code == 204

    me_response = await client.get("/auth/me")
    assert me_response.status_code == 401
