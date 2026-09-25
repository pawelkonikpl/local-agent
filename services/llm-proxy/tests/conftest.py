import uuid
from collections.abc import AsyncGenerator

import httpx
import pytest_asyncio
import sqlalchemy as sa
from httpx import ASGITransport, AsyncClient
from sqlalchemy.ext.asyncio import AsyncConnection, AsyncSession, async_sessionmaker, create_async_engine

import shared.db.models as shared_models
from llm_proxy.config import settings
from llm_proxy.db import get_db
from llm_proxy.main import app, get_anthropic_client, get_local_model_client, get_openai_client

# `shared.db.base.Base` is the exact same class api's models attach to (see libs/shared/src/shared/db/base.py),
# so its `.metadata` may hold api-owned tables too when both test suites run in one process. Scope DDL to just
# the tables this service actually owns, instead of a bare `Base.metadata.create_all()`/`drop_all()`.
_shared_metadata = shared_models.ChatSession.metadata
_shared_tables = [
    shared_models.ChatSession.__table__,
    shared_models.Message.__table__,
    shared_models.TokenUsage.__table__,
    shared_models.UserUsageLimit.__table__,
    shared_models.UserUsageCounter.__table__,
]

# llm-proxy never owns migrations and never imports `api` (see Etap 2 plan). Its tables have foreign
# keys into `users`, defined in `api`'s models -- stand in a minimal table here so those FKs resolve
# when this suite runs standalone, instead of pulling in the whole `api` package for test schema setup.
# A FK's referenced table is looked up on the *referencing* table's own MetaData (`_shared_metadata`
# here, since it's the same object as api's `Base.metadata`), so the stub must live there too, not in
# a separate MetaData -- and if api's test suite already registered the real `users` table there first
# (e.g. the combined `uv run pytest` run, where testpaths lists services/api/tests before this one),
# reuse that instead of redefining "users" a second time on the same registry, which SQLAlchemy rejects.
_users_table_owned_by_this_suite = "users" not in _shared_metadata.tables
if _users_table_owned_by_this_suite:
    _users_stub = sa.Table(
        "users",
        _shared_metadata,
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column("email", sa.String(), nullable=True),
        sa.Column("password_hash", sa.String(), nullable=True),
        sa.Column("role", sa.String(), nullable=True),
    )
else:
    _users_stub = _shared_metadata.tables["users"]


@pytest_asyncio.fixture(scope="session")
async def _schema() -> AsyncGenerator[None, None]:
    # This fixture drops these tables at teardown -- pointing it at the dev database (rather
    # than a dedicated one, e.g. `local_agent_test`, per README) silently wipes it.
    if "test" not in settings.database_url.rsplit("/", 1)[-1]:
        raise RuntimeError(
            f"DATABASE_URL ({settings.database_url!r}) doesn't look like a test database "
            "(expected the database name to contain 'test'). Refusing to create/drop schema "
            "against what looks like the dev database -- point DATABASE_URL at a dedicated "
            "test database (e.g. local_agent_test) before running pytest."
        )
    tables = [*_shared_tables, _users_stub] if _users_table_owned_by_this_suite else _shared_tables
    engine = create_async_engine(settings.database_url)
    async with engine.begin() as conn:
        await conn.run_sync(lambda sync_conn: _shared_metadata.create_all(sync_conn, tables=tables))
    yield
    async with engine.begin() as conn:
        # Plain drop_all() would fail here when the api test suite runs in the same process/DB:
        # api-owned tables (e.g. `tool_call_events`) still have live FKs into these tables at that
        # point, since these two test suites' session-scoped schema fixtures tear down independently.
        # CASCADE drops those foreign-key constraints along with these tables; it does not touch the
        # api-owned tables themselves, which api's own fixture cleans up separately. Only drop `users`
        # here if this suite created the stub itself -- if it's api's real table, that's api's to drop.
        table_names = ", ".join(t.name for t in tables)
        await conn.execute(sa.text(f"DROP TABLE IF EXISTS {table_names} CASCADE"))
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
async def db_session(db_connection: AsyncConnection) -> AsyncGenerator[AsyncSession, None]:
    session_factory = async_sessionmaker(
        bind=db_connection, expire_on_commit=False, join_transaction_mode="create_savepoint"
    )
    async with session_factory() as session:
        yield session


@pytest_asyncio.fixture
async def seed_user(db_connection: AsyncConnection) -> AsyncGenerator[uuid.UUID, None]:
    user_id = uuid.uuid4()
    await db_connection.execute(
        sa.insert(_users_stub).values(
            id=user_id, email=f"{user_id}@llm-proxy.test", password_hash="x", role="user"
        )
    )
    yield user_id


def _unused_anthropic_handler(request: httpx.Request) -> httpx.Response:
    raise AssertionError("Anthropic client should not be called in this test")


@pytest_asyncio.fixture
async def client(db_session: AsyncSession) -> AsyncGenerator[AsyncClient, None]:
    async def _override_get_db() -> AsyncGenerator[AsyncSession, None]:
        yield db_session

    async def _override_get_anthropic_client() -> httpx.AsyncClient:
        # `get_anthropic_client` is a plain Depends() resolved for every request regardless of what
        # the route body ends up doing, so it must resolve even in tests that never expect it to be
        # called (e.g. an auth-failure test) -- individual tests override this again when they need
        # specific upstream behavior.
        return httpx.AsyncClient(
            transport=httpx.MockTransport(_unused_anthropic_handler), base_url="https://unused.test"
        )

    async def _override_get_openai_client() -> None:
        # None == "not configured", same as a real deployment without OPENAI_API_KEY set. Tests
        # exercising the OpenAI-routed path override this again with a mocked client.
        return None

    async def _override_get_local_model_client() -> None:
        return None

    app.dependency_overrides[get_db] = _override_get_db
    app.dependency_overrides[get_anthropic_client] = _override_get_anthropic_client
    app.dependency_overrides[get_openai_client] = _override_get_openai_client
    app.dependency_overrides[get_local_model_client] = _override_get_local_model_client
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as ac:
        yield ac
    app.dependency_overrides.clear()
