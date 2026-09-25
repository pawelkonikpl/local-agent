from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

import httpx
from fastapi import FastAPI
from sqlalchemy import text

from api.admin.routes import router as admin_router
from api.auth.routes import router as auth_router
from api.chat.routes import router as chat_router
from api.config import settings
from api.db.session import engine


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    async with engine.connect() as conn:
        await conn.execute(text("SELECT 1"))
    app.state.llm_proxy_client = httpx.AsyncClient(
        base_url=settings.llm_proxy_url, timeout=httpx.Timeout(10.0, read=None)
    )
    yield
    await app.state.llm_proxy_client.aclose()


def create_app() -> FastAPI:
    app = FastAPI(title="local-agent api", lifespan=lifespan)
    app.include_router(auth_router)
    app.include_router(admin_router)
    app.include_router(chat_router)
    return app


app = create_app()


if __name__ == "__main__":
    import uvicorn

    uvicorn.run("api.main:app", host="0.0.0.0", port=8000, reload=True)
