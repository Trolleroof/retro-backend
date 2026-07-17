import json
from collections.abc import AsyncIterator
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
from httpx import ASGITransport, AsyncClient

ROOT = Path(__file__).resolve().parents[1]
V2_FIXTURE = ROOT / "contracts/fixtures/interaction-trace-chunk-v2.json"


class FakeD1PreparedStatement:
    def __init__(self, database: "FakeD1", sql: str) -> None:
        self.database = database
        self.sql = sql

    def bind(self, *values: object) -> "FakeD1PreparedStatement":
        self.database.bind_calls.append(values)
        return self

    async def first(self) -> object | None:
        self.database.first_calls.append(self.sql)
        if not self.database.first_rows:
            return None
        return self.database.first_rows.pop(0)

    async def run(self) -> object:
        self.database.run_calls.append(self.sql)
        rows = self.database.run_rows.pop(0) if self.database.run_rows else []
        return {"results": rows}


class FakeD1:
    def __init__(self) -> None:
        self.prepared_sql: list[str] = []
        self.bind_calls: list[tuple[object, ...]] = []
        self.first_calls: list[str] = []
        self.run_calls: list[str] = []
        self.first_rows: list[object | None] = []
        self.run_rows: list[list[object]] = []

    def prepare(self, sql: str) -> FakeD1PreparedStatement:
        self.prepared_sql.append(sql)
        return FakeD1PreparedStatement(self, sql)


@pytest.fixture
def fixture_bytes() -> bytes:
    return V2_FIXTURE.read_bytes()


@pytest.fixture
def fixture_payload(fixture_bytes: bytes) -> dict[str, Any]:
    return json.loads(fixture_bytes)


@pytest.fixture
def fake_d1() -> FakeD1:
    return FakeD1()


@pytest.fixture
def fake_env(fake_d1: FakeD1) -> SimpleNamespace:
    return SimpleNamespace(DB=fake_d1)


@pytest.fixture
async def app_client(fake_env: SimpleNamespace) -> AsyncIterator[AsyncClient]:
    from retro_backend.app import app

    app.state.env = fake_env
    async with AsyncClient(
        transport=ASGITransport(app=app),
        base_url="http://test",
    ) as client:
        yield client
    del app.state.env
