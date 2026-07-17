"""FastAPI application factory and production entrypoint."""

from contextlib import AsyncExitStack, asynccontextmanager

from fastapi import FastAPI
from prometheus_client import make_asgi_app
from redis.asyncio import Redis
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from .api.routes import router
from .config import Settings, get_settings
from .database import create_database_engine, create_session_factory
from .producer import EventProducer, KafkaEventProducer
from .redis_client import create_redis


def create_app(
    *,
    settings: Settings | None = None,
    session_factory: async_sessionmaker[AsyncSession] | None = None,
    redis: Redis | None = None,
    producer: EventProducer | None = None,
) -> FastAPI:
    resolved = settings or get_settings()
    owned_engine = None
    if session_factory is None:
        owned_engine = create_database_engine(resolved)
        session_factory = create_session_factory(owned_engine)

    owned_redis = redis is None
    owned_producer = producer is None
    resolved_redis = redis if redis is not None else create_redis(resolved)
    resolved_producer = producer if producer is not None else KafkaEventProducer(resolved)

    @asynccontextmanager
    async def lifespan(_: FastAPI):
        async with AsyncExitStack() as stack:
            if owned_engine is not None:
                stack.push_async_callback(owned_engine.dispose)
            if owned_redis and resolved_redis is not None:
                stack.push_async_callback(resolved_redis.aclose)
            if owned_producer:
                stack.push_async_callback(resolved_producer.stop)
                await resolved_producer.start()
            yield

    app = FastAPI(
        title="Retro Backend",
        description="Authenticated gameplay event production and personal best API",
        version="0.1.0",
        lifespan=lifespan,
    )
    app.state.settings = resolved
    app.state.session_factory = session_factory
    app.state.redis = resolved_redis
    app.state.producer = resolved_producer
    app.include_router(router)
    app.mount("/metrics", make_asgi_app())
    return app


app = create_app()
