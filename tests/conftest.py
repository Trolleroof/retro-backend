import hashlib
import json
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any

import pytest
from fakeredis.aioredis import FakeRedis
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient
from sqlalchemy import event
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.pool import StaticPool

from retro_backend.config import Settings
from retro_backend.models import Base, User
from retro_backend.producer import BrokerMetadata

ROOT = Path(__file__).resolve().parents[1]
V2_FIXTURE = ROOT / "contracts/fixtures/interaction-trace-chunk-v2.json"


def _enable_sqlite_foreign_keys(dbapi_connection: Any, _: Any) -> None:
    cursor = dbapi_connection.cursor()
    cursor.execute("PRAGMA foreign_keys=ON")
    cursor.close()


@pytest.fixture
def fixture_bytes() -> bytes:
    return V2_FIXTURE.read_bytes()


@pytest.fixture
def fixture_payload(fixture_bytes: bytes) -> dict[str, Any]:
    return json.loads(fixture_bytes)


@pytest.fixture
async def session_factory() -> AsyncIterator[async_sessionmaker[AsyncSession]]:
    engine = create_async_engine(
        "sqlite+aiosqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    event.listen(engine.sync_engine, "connect", _enable_sqlite_foreign_keys)
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    yield factory
    await engine.dispose()


@pytest.fixture
async def app(
    session_factory: async_sessionmaker[AsyncSession],
    fake_redis: FakeRedis,
    producer: "RecordingProducer",
    auth_token: str,
    fixture_payload: dict[str, Any],
) -> FastAPI:
    from retro_backend.main import create_app

    async with session_factory() as session:
        session.add(
            User(
                id=fixture_payload["user_id"],
                auth_token_hash=hashlib.sha256(auth_token.encode()).hexdigest(),
            )
        )
        await session.commit()
    settings = Settings(
        environment="test",
        database_url="sqlite+aiosqlite://",
        redis_url=None,
    )
    return create_app(
        settings=settings,
        session_factory=session_factory,
        redis=fake_redis,
        producer=producer,
    )


@pytest.fixture
async def client(app: FastAPI) -> AsyncIterator[AsyncClient]:
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as value:
        yield value


@pytest.fixture
def auth_token() -> str:
    return "retro_test-secret"


@pytest.fixture
def auth_headers(auth_token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {auth_token}", "Content-Type": "application/json"}


@pytest.fixture
def fake_redis() -> FakeRedis:
    return FakeRedis(decode_responses=True)


class RecordingProducer:
    def __init__(self) -> None:
        self.calls: list[dict[str, Any]] = []
        self.failure: Exception | None = None
        self.started = False
        self.stopped = False
        self.healthy = True
        self.health_calls = 0

    async def start(self) -> None:
        self.started = True

    async def stop(self) -> None:
        self.stopped = True

    async def healthcheck(self) -> bool:
        self.health_calls += 1
        return self.healthy

    async def send(
        self,
        *,
        topic: str,
        value: bytes,
        key: bytes,
        headers: list[tuple[str, bytes]],
    ) -> BrokerMetadata:
        self.calls.append({"topic": topic, "value": value, "key": key, "headers": headers})
        if self.failure is not None:
            raise self.failure
        return BrokerMetadata(partition=3, offset=42)


@pytest.fixture
def producer() -> RecordingProducer:
    return RecordingProducer()
