from collections.abc import AsyncGenerator

import api.db.models  # noqa: F401 -- populates Base.metadata
import pytest_asyncio
from api.config import settings
from api.db.session import get_db, get_session_factory
from api.main import app
from httpx import ASGITransport, AsyncClient
from shared.db.base import Base
from sqlalchemy.ext.asyncio import (
    AsyncConnection,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)


@pytest_asyncio.fixture(scope="session")
async def _schema() -> AsyncGenerator[None, None]:
    # This fixture drops the whole schema at teardown -- pointing it at the dev database
    # (rather than a dedicated one, e.g. `local_agent_test`, per README) silently wipes it.
    if "test" not in settings.database_url.rsplit("/", 1)[-1]:
        raise RuntimeError(
            f"DATABASE_URL ({settings.database_url!r}) doesn't look like a test database "
            "(expected the database name to contain 'test'). Refusing to create/drop schema "
            "against what looks like the dev database -- point DATABASE_URL at a dedicated "
            "test database (e.g. local_agent_test) before running pytest."
        )
    engine = create_async_engine(settings.database_url)
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    yield
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.drop_all)
    await engine.dispose()


@pytest_asyncio.fixture
async def db_connection(_schema: None) -> AsyncGenerator[AsyncConnection, None]:
    engine = create_async_engine(settings.database_url)
    async with engine.connect() as conn:
        trans = await conn.begin()
        yield conn
        await trans.rollback()
    await engine.dispose()


@pytest_asyncio.fixture
def session_factory(db_connection: AsyncConnection) -> async_sessionmaker[AsyncSession]:
    """Sessions inside the test's outer transaction, rolled back at the end of the test."""
    return async_sessionmaker(bind=db_connection, expire_on_commit=False, join_transaction_mode="create_savepoint")


@pytest_asyncio.fixture
async def db_session(session_factory: async_sessionmaker[AsyncSession]) -> AsyncGenerator[AsyncSession, None]:
    async with session_factory() as session:
        yield session


@pytest_asyncio.fixture
async def client(
    db_session: AsyncSession, session_factory: async_sessionmaker[AsyncSession]
) -> AsyncGenerator[AsyncClient, None]:
    async def _override_get_db() -> AsyncGenerator[AsyncSession, None]:
        yield db_session

    app.dependency_overrides[get_db] = _override_get_db
    # Chat generations write through their own sessions; keep those in the test transaction too.
    app.dependency_overrides[get_session_factory] = lambda: session_factory
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as ac:
        yield ac
    app.dependency_overrides.clear()
