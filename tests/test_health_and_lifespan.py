import pytest
from fakeredis.aioredis import FakeRedis
from httpx import AsyncClient
from redis.exceptions import RedisError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from retro_backend import main
from retro_backend.config import Settings
from retro_backend.producer import BrokerMetadata


async def test_health_endpoints_remain_available(client: AsyncClient) -> None:
    live = await client.get("/health/live")
    ready = await client.get("/health/ready")
    assert live.status_code == 200
    assert ready.status_code == 200


async def test_readiness_returns_503_when_redis_fails(
    client: AsyncClient,
    fake_redis: FakeRedis,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def failed_ping() -> bool:
        raise RedisError("unavailable")

    monkeypatch.setattr(fake_redis, "ping", failed_ping)
    response = await client.get("/health/ready")
    assert response.status_code == 503
    assert response.json()["detail"] == "redis unavailable"


async def test_owned_producer_stops_when_lifespan_body_raises(
    session_factory: async_sessionmaker[AsyncSession],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class LifecycleProducer:
        def __init__(self, _: Settings) -> None:
            self.started = False
            self.stopped = False

        async def start(self) -> None:
            self.started = True

        async def stop(self) -> None:
            self.stopped = True

        async def send(
            self,
            *,
            topic: str,
            value: bytes,
            key: bytes,
            headers: list[tuple[str, bytes]],
        ) -> BrokerMetadata:
            _ = topic, value, key, headers
            raise AssertionError("not used")

    producer = LifecycleProducer(Settings())

    def create_producer(_: Settings) -> LifecycleProducer:
        return producer

    monkeypatch.setattr(main, "KafkaEventProducer", create_producer)
    app = main.create_app(
        settings=Settings(environment="test", database_url="sqlite+aiosqlite://", redis_url=None),
        session_factory=session_factory,
    )

    with pytest.raises(RuntimeError, match="lifespan failure"):
        async with app.router.lifespan_context(app):
            assert producer.started is True
            raise RuntimeError("lifespan failure")
    assert producer.stopped is True
