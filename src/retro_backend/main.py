"""FastAPI application factory and production entrypoint."""

from contextlib import asynccontextmanager

from fastapi import FastAPI
from prometheus_client import make_asgi_app
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from .api.routes import router
from .config import Settings, get_settings
from .database import create_database_engine, create_session_factory
from .redis_client import create_redis


def create_app(
    *,
    settings: Settings | None = None,
    session_factory: async_sessionmaker[AsyncSession] | None = None,
) -> FastAPI:
    resolved = settings or get_settings()
    owned_engine = None
    if session_factory is None:
        owned_engine = create_database_engine(resolved)
        session_factory = create_session_factory(owned_engine)

    @asynccontextmanager
    async def lifespan(_: FastAPI):
        redis = create_redis(resolved)
        app.state.redis = redis
        yield
        if redis is not None:
            await redis.aclose()
        if owned_engine is not None:
            await owned_engine.dispose()

    app = FastAPI(
        title="Retro Backend",
        description="Gameplay ingestion, storage, processing, and query API",
        version="0.1.0",
        lifespan=lifespan,
    )
    app.state.settings = resolved
    app.state.session_factory = session_factory
    app.state.redis = None
    app.include_router(router)
    app.mount("/metrics", make_asgi_app())
    return app


app = create_app()
