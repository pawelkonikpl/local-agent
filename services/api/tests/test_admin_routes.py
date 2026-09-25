from httpx import AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession

from api.auth.security import hash_password
from api.db.models.user import User


async def _create_user(db_session: AsyncSession, email: str, password: str, role: str) -> User:
    user = User(email=email, password_hash=hash_password(password), role=role)
    db_session.add(user)
    await db_session.flush()
    return user


async def test_create_user_as_admin_returns_201(client: AsyncClient, db_session: AsyncSession) -> None:
    await _create_user(db_session, "root@example.com", "root-pass-123", "admin")
    await client.post("/auth/login", json={"email": "root@example.com", "password": "root-pass-123"})

    response = await client.post(
        "/admin/users",
        json={"email": "newuser@example.com", "password": "newuser-pass-123", "role": "user"},
    )

    assert response.status_code == 201
    assert response.json()["role"] == "user"


async def test_create_user_as_regular_user_returns_403(
    client: AsyncClient, db_session: AsyncSession
) -> None:
    await _create_user(db_session, "grace@example.com", "grace-pass-123", "user")
    await client.post("/auth/login", json={"email": "grace@example.com", "password": "grace-pass-123"})

    response = await client.post(
        "/admin/users",
        json={"email": "someone@example.com", "password": "someone-pass-123", "role": "user"},
    )

    assert response.status_code == 403


async def test_create_user_without_auth_returns_401(client: AsyncClient) -> None:
    response = await client.post(
        "/admin/users",
        json={"email": "someone@example.com", "password": "someone-pass-123", "role": "user"},
    )
    assert response.status_code == 401


async def test_create_user_with_duplicate_email_returns_409(
    client: AsyncClient, db_session: AsyncSession
) -> None:
    await _create_user(db_session, "heidi@example.com", "heidi-pass-123", "admin")
    await client.post("/auth/login", json={"email": "heidi@example.com", "password": "heidi-pass-123"})

    response = await client.post(
        "/admin/users",
        json={"email": "heidi@example.com", "password": "another-pass-123", "role": "user"},
    )

    assert response.status_code == 409
