import json
from collections.abc import AsyncIterator, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
from httpx import ASGITransport, AsyncClient

ROOT = Path(__file__).resolve().parents[1]
V2_FIXTURE = ROOT / "contracts/fixtures/interaction-trace-chunk-v2.json"
V2_CONTINUATION_FIXTURE = (
    ROOT / "contracts/fixtures/interaction-trace-chunk-v2-continuation.json"
)


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
        self.run_rows: list[object] = []

    def prepare(self, sql: str) -> FakeD1PreparedStatement:
        self.prepared_sql.append(sql)
        return FakeD1PreparedStatement(self, sql)


@dataclass(frozen=True, slots=True)
class FakeR2PutCall:
    key: str
    body: object
    options: dict[str, object]


class FakeJsArrayBuffer:
    def __init__(self, body: bytes) -> None:
        self.body = body
        self.to_bytes_calls = 0

    def to_bytes(self) -> bytes:
        self.to_bytes_calls += 1
        return self.body


class FakeR2Object:
    def __init__(self, body: object, custom_metadata: object) -> None:
        self._body = body
        self.customMetadata = custom_metadata
        self.array_buffer_calls = 0

    async def arrayBuffer(self) -> object:
        self.array_buffer_calls += 1
        return self._body


class FakeR2Bucket:
    def __init__(self, operation_order: list[str]) -> None:
        self.operation_order = operation_order
        self.objects: dict[str, object] = {}
        self.head_calls: list[str] = []
        self.head_results: list[object | None] = []
        self.get_calls: list[str] = []
        self.get_results: list[object | None] = []
        self.put_calls: list[FakeR2PutCall] = []
        self.put_results: list[object | None] = []
        self.head_failure: Exception | None = None
        self.get_failure: Exception | None = None
        self.get_failures: dict[str, Exception] = {}
        self.put_failure: Exception | None = None

    async def head(self, key: str) -> object | None:
        self.head_calls.append(key)
        if self.head_failure is not None:
            raise self.head_failure
        if self.head_results:
            return self.head_results.pop(0)
        return self.objects.get(key)

    async def get(self, key: str) -> object | None:
        self.operation_order.append("r2.get")
        self.get_calls.append(key)
        if key in self.get_failures:
            raise self.get_failures[key]
        if self.get_failure is not None:
            raise self.get_failure
        if self.get_results:
            return self.get_results.pop(0)
        return self.objects.get(key)

    async def put(
        self,
        key: str,
        value: object,
        options: object | None = None,
    ) -> object | None:
        assert isinstance(options, dict)
        self.operation_order.append("r2.put")
        self.put_calls.append(FakeR2PutCall(key=key, body=value, options=options))
        if self.put_failure is not None:
            raise self.put_failure
        if self.put_results:
            result = self.put_results.pop(0)
            if result is not None:
                self.objects[key] = result
            return result

        stored = FakeR2Object(value, options["customMetadata"])
        self.objects[key] = stored
        return stored

    def seed_raw(
        self,
        key: str,
        body: bytes,
        payload_sha256: str | None,
        *,
        proxy_metadata: bool = False,
        proxy_buffer: bool = False,
    ) -> FakeR2Object:
        metadata: object
        if proxy_metadata:
            metadata = SimpleNamespace(payload_sha256=payload_sha256)
        else:
            metadata = (
                {} if payload_sha256 is None else {"payload_sha256": payload_sha256}
            )
        buffer: object = FakeJsArrayBuffer(body) if proxy_buffer else body
        stored = FakeR2Object(buffer, metadata)
        self.objects[key] = stored
        return stored

    def seed_metadata(
        self,
        key: str,
        payload_sha256: str | None,
        *,
        proxy: bool = False,
    ) -> object:
        metadata: object
        if proxy:
            metadata = SimpleNamespace(payload_sha256=payload_sha256)
        else:
            metadata = (
                {} if payload_sha256 is None else {"payload_sha256": payload_sha256}
            )
        stored = FakeR2Object(b"", metadata)
        self.objects[key] = stored
        return stored


class FakeQueueMessage:
    def __init__(
        self,
        body: object,
        *,
        message_id: str = "msg_01",
        timestamp: object | None = None,
        operation_order: list[str] | None = None,
    ) -> None:
        self.body = body
        self.id = message_id
        self.timestamp = (
            timestamp if timestamp is not None else datetime(2026, 7, 17, tzinfo=UTC)
        )
        self.operation_order = operation_order if operation_order is not None else []
        self.ack_calls = 0
        self.retry_calls = 0

    @property
    def acked(self) -> bool:
        return self.ack_calls > 0

    @property
    def retried(self) -> bool:
        return self.retry_calls > 0

    def ack(self) -> None:
        self.operation_order.append(f"ack:{self.id}")
        self.ack_calls += 1

    def retry(self) -> None:
        self.operation_order.append(f"retry:{self.id}")
        self.retry_calls += 1


class FakeQueueBatch:
    def __init__(self, queue: str, messages: Sequence[object]) -> None:
        self.queue = queue
        self.messages = messages


@pytest.fixture
def queue_message_factory() -> type[FakeQueueMessage]:
    return FakeQueueMessage


@pytest.fixture
def queue_batch_factory() -> type[FakeQueueBatch]:
    return FakeQueueBatch


@pytest.fixture
def array_buffer_factory() -> type[FakeJsArrayBuffer]:
    return FakeJsArrayBuffer


class FakeQueueBinding:
    def __init__(self, operation_order: list[str]) -> None:
        self.operation_order = operation_order
        self.send_calls: list[object] = []
        self.failure: Exception | None = None

    async def send(self, message: object) -> object:
        self.operation_order.append("queue.send")
        self.send_calls.append(message)
        if self.failure is not None:
            raise self.failure
        return {"accepted": True}


class FakeRateLimiter:
    def __init__(self) -> None:
        self.success = True
        self.return_proxy = False
        self.calls: list[dict[str, str]] = []
        self.failure: Exception | None = None

    async def limit(self, options: dict[str, str]) -> object:
        self.calls.append(options)
        if self.failure is not None:
            raise self.failure
        if self.return_proxy:
            return SimpleNamespace(success=self.success)
        return {"success": self.success}


class FakeAuthCache:
    def __init__(self) -> None:
        self.values: dict[str, str] = {}
        self.get_calls: list[tuple[str, str]] = []
        self.put_calls: list[tuple[str, str, str]] = []
        self.get_failure: Exception | None = None
        self.put_failure: Exception | None = None

    async def get(self, request_url: str, token_digest: str) -> str | None:
        self.get_calls.append((request_url, token_digest))
        if self.get_failure is not None:
            raise self.get_failure
        return self.values.get(token_digest)

    async def put(
        self,
        request_url: str,
        token_digest: str,
        user_id: str,
    ) -> None:
        self.put_calls.append((request_url, token_digest, user_id))
        if self.put_failure is not None:
            raise self.put_failure
        self.values[token_digest] = user_id


class FakeChunkGateStub:
    def __init__(self, operation_order: list[str], rpc_order: list[str]) -> None:
        self.operation_order = operation_order
        self.rpc_order = rpc_order
        self.reserve_result: object | None = None
        self.reserve_calls: list[tuple[str, str, int, int]] = []
        self.accept_calls: list[tuple[str, str, object, int, int]] = []
        self.release_calls: list[tuple[str, str]] = []
        self.reserve_failure: Exception | None = None
        self.accept_failure: Exception | None = None
        self.release_failure: Exception | None = None

    async def reserve(
        self,
        payload_sha256: str,
        lease_token: str,
        now_ms: int,
        lease_ms: int,
    ) -> object:
        self.operation_order.append("gate.reserve")
        self.rpc_order.append("reserve")
        self.reserve_calls.append((payload_sha256, lease_token, now_ms, lease_ms))
        if self.reserve_failure is not None:
            raise self.reserve_failure
        if self.reserve_result is not None:
            return self.reserve_result
        return {"status": "reserved", "lease_token": lease_token}

    async def accept(
        self,
        payload_sha256: str,
        lease_token: str,
        acknowledgment: object,
        now_ms: int,
        dedupe_ttl_ms: int,
    ) -> object:
        self.operation_order.append("gate.accept")
        self.rpc_order.append("accept")
        self.accept_calls.append(
            (
                payload_sha256,
                lease_token,
                acknowledgment,
                now_ms,
                dedupe_ttl_ms,
            )
        )
        if self.accept_failure is not None:
            raise self.accept_failure
        return {"accepted": True}

    async def release(self, payload_sha256: str, lease_token: str) -> object:
        self.operation_order.append("gate.release")
        self.rpc_order.append("release")
        self.release_calls.append((payload_sha256, lease_token))
        if self.release_failure is not None:
            raise self.release_failure
        return {"released": True}


class FakeChunkGateNamespace:
    def __init__(self, operation_order: list[str]) -> None:
        self.rpc_order: list[str] = []
        self.id_from_name_calls: list[str] = []
        self.get_calls: list[object] = []
        self.stub = FakeChunkGateStub(operation_order, self.rpc_order)

    def idFromName(self, name: str) -> object:
        self.rpc_order.append("idFromName")
        self.id_from_name_calls.append(name)
        return SimpleNamespace(name=name)

    def get(self, object_id: object) -> FakeChunkGateStub:
        self.rpc_order.append("get")
        self.get_calls.append(object_id)
        return self.stub

    @property
    def reserve_result(self) -> object | None:
        return self.stub.reserve_result

    @reserve_result.setter
    def reserve_result(self, value: object) -> None:
        self.stub.reserve_result = value

    @property
    def release_calls(self) -> list[tuple[str, str]]:
        return self.stub.release_calls


@pytest.fixture
def fixture_bytes() -> bytes:
    return V2_FIXTURE.read_bytes()


@pytest.fixture
def fixture_payload(fixture_bytes: bytes) -> dict[str, Any]:
    return json.loads(fixture_bytes)


@pytest.fixture
def continuation_fixture_bytes() -> bytes:
    return V2_CONTINUATION_FIXTURE.read_bytes()


@pytest.fixture
def continuation_fixture_payload(
    continuation_fixture_bytes: bytes,
) -> dict[str, Any]:
    return json.loads(continuation_fixture_bytes)


@pytest.fixture
def fake_d1() -> FakeD1:
    return FakeD1()


@pytest.fixture
def fake_env(fake_d1: FakeD1) -> SimpleNamespace:
    operation_order: list[str] = []
    return SimpleNamespace(
        DB=fake_d1,
        AUTH_CACHE=FakeAuthCache(),
        RAW_BUCKET=FakeR2Bucket(operation_order),
        GAMEPLAY_QUEUE=FakeQueueBinding(operation_order),
        CHUNK_GATE=FakeChunkGateNamespace(operation_order),
        INGEST_RATE_LIMITER=FakeRateLimiter(),
        MAX_CHUNK_BYTES="262144",
        MAX_RESULT_BYTES="16384",
        CHUNK_DEDUP_TTL_MS="172800000",
        CHUNK_PUBLISHING_LEASE_MS="30000",
        operation_order=operation_order,
    )


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
