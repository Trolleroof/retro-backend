import json
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any

import pytest
from httpx import ASGITransport, AsyncClient
from sqlalchemy import event
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.pool import StaticPool

from retro_backend.config import Settings
from retro_backend.main import create_app
from retro_backend.models import Base

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
async def client(
    session_factory: async_sessionmaker[AsyncSession],
) -> AsyncIterator[AsyncClient]:
    settings = Settings(
        environment="test",
        database_url="sqlite+aiosqlite://",
        ingest_api_key="test-secret",
        redis_url=None,
    )
    app = create_app(settings=settings, session_factory=session_factory)
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as value:
        yield value


@pytest.fixture
def auth_headers() -> dict[str, str]:
    return {"Authorization": "Bearer test-secret", "Content-Type": "application/json"}
