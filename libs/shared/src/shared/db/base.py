from sqlalchemy.orm import DeclarativeBase


class Base(DeclarativeBase):
    """Single metadata registry shared by `api` (migration owner) and `llm-proxy`.

    Both services' ORM models must attach to this same `Base` so foreign keys between
    api-owned tables (e.g. `users`) and shared tables (e.g. `sessions`) resolve within
    one `MetaData`, even though only `api` ever runs Alembic against it.
    """
