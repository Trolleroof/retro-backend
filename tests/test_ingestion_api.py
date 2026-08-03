import hashlib
import json
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any, cast

import pytest
from fastapi import Request
from httpx import AsyncClient
from jsonschema import Draft202012Validator

import retro_backend.api.request_body as request_body_module
from retro_backend.api.request_body import BodyTooLarge, read_limited_body

ROOT = Path(__file__).resolve().parents[1]
INGEST_RESPONSE_SCHEMA = json.loads(
    (ROOT / "contracts/ingest-response-v1.schema.json").read_text()
)


def _authorize(fake_d1: Any, fixture_payload: dict[str, Any]) -> None:
    fake_d1.first_rows.append({"id": fixture_payload["user_id"]})


def _raw_key(payload: dict[str, Any]) -> str:
    return (
        f"raw/user={payload['user_id']}/game={payload['game_id']}"
        f"/chunk={payload['chunk_id']}.json"
    )


@pytest.mark.parametrize(
    "headers",
    [
        {},
        {"Authorization": "bearer opaque"},
        {"Authorization": "Bearer opaque extra"},
        {"Authorization": "Bearer unknown-session"},
    ],
    ids=["missing", "lowercase-scheme", "malformed", "unknown-session"],
)
async def test_rejected_auth_returns_401_without_storing_or_enqueueing(
    app_client: AsyncClient,
    fake_env: Any,
    fixture_bytes: bytes,
    headers: dict[str, str],
) -> None:
    response = await app_client.post(
        "/v1/game-event-chunks",
        content=fixture_bytes,
        headers=headers,
    )

    assert response.status_code == 401
    assert fake_env.operation_order == []
    assert fake_env.RAW_BUCKET.put_calls == []
    assert fake_env.GAMEPLAY_QUEUE.send_calls == []
    assert fake_env.CHUNK_GATE.id_from_name_calls == []
    assert fake_env.INGEST_RATE_LIMITER.calls == []


async def test_chunk_route_returns_ack_matching_json_schema(
    app_client: AsyncClient,
    fake_d1: Any,
    fake_env: Any,
    fixture_bytes: bytes,
    fixture_payload: dict[str, Any],
) -> None:
    _authorize(fake_d1, fixture_payload)
    fake_env.INGEST_RATE_LIMITER.return_proxy = True

    response = await app_client.post(
        "/v1/game-event-chunks",
        content=fixture_bytes,
        headers={"Authorization": "Bearer opaque"},
    )

    assert response.status_code == 202
    Draft202012Validator.check_schema(INGEST_RESPONSE_SCHEMA)
    Draft202012Validator(INGEST_RESPONSE_SCHEMA).validate(response.json())
    assert response.json() == {
        "accepted": True,
        "duplicate": False,
        "game_id": fixture_payload["game_id"],
        "chunk_id": fixture_payload["chunk_id"],
        "chunk_sequence": fixture_payload["chunk_sequence"],
        "accepted_event_range": {
            "first": fixture_payload["first_event_sequence"],
            "last": fixture_payload["last_event_sequence"],
        },
        "queued": True,
        "server_received_at_ms": response.json()["server_received_at_ms"],
    }
    assert "broker_partition" not in response.json()
    assert "broker_offset" not in response.json()
    assert fake_env.INGEST_RATE_LIMITER.calls == [
        {"key": f"{fixture_payload['user_id']}:game-event-chunks"}
    ]


async def test_duplicate_chunk_returns_idempotent_ack_matching_json_schema(
    app_client: AsyncClient,
    fake_d1: Any,
    fake_env: Any,
    fixture_bytes: bytes,
    fixture_payload: dict[str, Any],
) -> None:
    _authorize(fake_d1, fixture_payload)
    cached_ack = {
        "accepted": True,
        "duplicate": False,
        "game_id": fixture_payload["game_id"],
        "chunk_id": fixture_payload["chunk_id"],
        "chunk_sequence": fixture_payload["chunk_sequence"],
        "accepted_event_range": {
            "first": fixture_payload["first_event_sequence"],
            "last": fixture_payload["last_event_sequence"],
        },
        "queued": True,
        "server_received_at_ms": 1_784_241_600_000,
    }
    fake_env.CHUNK_GATE.reserve_result = {"status": "duplicate", "ack": cached_ack}

    response = await app_client.post(
        "/v1/game-event-chunks",
        content=fixture_bytes,
        headers={"Authorization": "Bearer opaque"},
    )

    assert response.status_code == 202
    Draft202012Validator(INGEST_RESPONSE_SCHEMA).validate(response.json())
    # Same ack the first request returned, with only `duplicate` flipped: the
    # response contract must not change for clients that already retry.
    assert response.json() == {**cached_ack, "duplicate": True}
    assert fake_env.operation_order == ["gate.reserve"]
    assert fake_env.RAW_BUCKET.put_calls == []
    assert fake_env.GAMEPLAY_QUEUE.send_calls == []


async def test_rate_limit_denial_returns_429_before_gate_or_storage(
    app_client: AsyncClient,
    fake_d1: Any,
    fake_env: Any,
    fixture_bytes: bytes,
    fixture_payload: dict[str, Any],
) -> None:
    _authorize(fake_d1, fixture_payload)
    fake_env.INGEST_RATE_LIMITER.success = False
    fake_env.INGEST_RATE_LIMITER.return_proxy = True

    response = await app_client.post(
        "/v1/game-event-chunks",
        content=fixture_bytes,
        headers={"Authorization": "Bearer opaque"},
    )

    assert response.status_code == 429
    assert response.headers["Retry-After"] == "60"
    assert response.json() == {"detail": "Rate limit exceeded"}
    assert fake_env.operation_order == []
    assert fake_env.CHUNK_GATE.id_from_name_calls == []
    assert fake_env.RAW_BUCKET.head_calls == []
    assert fake_env.GAMEPLAY_QUEUE.send_calls == []


@pytest.mark.parametrize("binding", ["INGEST_RATE_LIMITER", "RAW_BUCKET", "GAMEPLAY_QUEUE", "CHUNK_GATE"])
async def test_missing_ingest_binding_returns_safe_503(
    app_client: AsyncClient,
    fake_d1: Any,
    fake_env: Any,
    fixture_bytes: bytes,
    fixture_payload: dict[str, Any],
    binding: str,
) -> None:
    _authorize(fake_d1, fixture_payload)
    value = getattr(fake_env, binding)
    delattr(fake_env, binding)
    try:
        response = await app_client.post(
            "/v1/game-event-chunks",
            content=fixture_bytes,
            headers={"Authorization": "Bearer opaque"},
        )
    finally:
        setattr(fake_env, binding, value)

    assert response.status_code == 503
    assert response.json() == {"detail": "Event ingestion unavailable"}
    assert fake_env.operation_order == []


async def test_rate_limiter_failure_returns_safe_503(
    app_client: AsyncClient,
    fake_d1: Any,
    fake_env: Any,
    fixture_bytes: bytes,
    fixture_payload: dict[str, Any],
) -> None:
    _authorize(fake_d1, fixture_payload)
    fake_env.INGEST_RATE_LIMITER.failure = RuntimeError("proxy detail")

    response = await app_client.post(
        "/v1/game-event-chunks",
        content=fixture_bytes,
        headers={"Authorization": "Bearer opaque"},
    )

    assert response.status_code == 503
    assert response.json() == {"detail": "Event ingestion unavailable"}
    assert fake_env.operation_order == []


@pytest.mark.parametrize(
    ("body", "expected_detail"),
    [
        (b"", "request body is empty"),
        (b"{not-json", "invalid chunk envelope"),
    ],
)
async def test_empty_and_invalid_json_return_400_after_rate_limit(
    app_client: AsyncClient,
    fake_d1: Any,
    fake_env: Any,
    fixture_payload: dict[str, Any],
    body: bytes,
    expected_detail: str,
) -> None:
    _authorize(fake_d1, fixture_payload)

    response = await app_client.post(
        "/v1/game-event-chunks",
        content=body,
        headers={"Authorization": "Bearer opaque"},
    )

    assert response.status_code == 400
    assert response.json() == {"detail": expected_detail}
    assert fake_env.INGEST_RATE_LIMITER.calls == [
        {"key": f"{fixture_payload['user_id']}:game-event-chunks"}
    ]
    assert fake_env.operation_order == []


async def test_deeply_nested_json_under_limit_returns_safe_400(
    app_client: AsyncClient,
    fake_d1: Any,
    fake_env: Any,
    fixture_payload: dict[str, Any],
) -> None:
    _authorize(fake_d1, fixture_payload)
    body = (b"[" * 10_000) + b"0" + (b"]" * 10_000)
    assert len(body) < 262_144

    response = await app_client.post(
        "/v1/game-event-chunks",
        content=body,
        headers={"Authorization": "Bearer opaque"},
    )

    assert response.status_code == 400
    assert response.json() == {"detail": "invalid chunk envelope"}
    assert fake_env.operation_order == []


async def test_schema_v1_returns_400(
    app_client: AsyncClient,
    fake_d1: Any,
    fake_env: Any,
    fixture_payload: dict[str, Any],
) -> None:
    _authorize(fake_d1, fixture_payload)
    fixture_payload["schema_version"] = 1

    response = await app_client.post(
        "/v1/game-event-chunks",
        content=json.dumps(fixture_payload).encode(),
        headers={"Authorization": "Bearer opaque"},
    )

    assert response.status_code == 400
    assert response.json() == {"detail": "invalid chunk envelope"}
    assert fake_env.operation_order == []


@pytest.mark.parametrize(
    ("field", "prefix"),
    [
        ("user_id", "usr_"),
        ("game_id", "game_"),
        ("chunk_id", "chk_"),
        ("event_id", "evt_"),
    ],
)
async def test_overlong_identifier_returns_400_before_storage(
    app_client: AsyncClient,
    fake_d1: Any,
    fake_env: Any,
    fixture_payload: dict[str, Any],
    field: str,
    prefix: str,
) -> None:
    _authorize(fake_d1, fixture_payload)
    value = f"{prefix}{'a' * 121}"
    if field == "event_id":
        fixture_payload["events"][1][field] = value
    else:
        fixture_payload[field] = value
        for event in fixture_payload["events"]:
            event[field] = value

    response = await app_client.post(
        "/v1/game-event-chunks",
        content=json.dumps(fixture_payload).encode(),
        headers={"Authorization": "Bearer opaque"},
    )

    assert response.status_code == 400
    assert response.json() == {"detail": "invalid chunk envelope"}
    assert fake_env.operation_order == []


async def test_fractional_created_at_ms_returns_400_before_storage(
    app_client: AsyncClient,
    fake_d1: Any,
    fake_env: Any,
    fixture_payload: dict[str, Any],
) -> None:
    _authorize(fake_d1, fixture_payload)
    fixture_payload["created_at_ms"] += 0.5

    response = await app_client.post(
        "/v1/game-event-chunks",
        content=json.dumps(fixture_payload).encode(),
        headers={"Authorization": "Bearer opaque"},
    )

    assert response.status_code == 400
    assert response.json() == {"detail": "invalid chunk envelope"}
    assert fake_env.operation_order == []


async def test_authenticated_identity_mismatch_returns_400(
    app_client: AsyncClient,
    fake_d1: Any,
    fake_env: Any,
    fixture_payload: dict[str, Any],
) -> None:
    authenticated_user = fixture_payload["user_id"]
    fake_d1.first_rows.append({"id": authenticated_user})
    fixture_payload["user_id"] = "usr_someone_else"

    response = await app_client.post(
        "/v1/game-event-chunks",
        content=json.dumps(fixture_payload).encode(),
        headers={"Authorization": "Bearer opaque"},
    )

    assert response.status_code == 400
    assert response.json() == {
        "detail": (
            "user_id must match the authenticated user "
            f"(expected {authenticated_user}, received usr_someone_else); "
            "use the user_id returned by GET /v1/me"
        )
    }
    assert fake_env.operation_order == []


async def test_content_length_above_default_limit_returns_413_without_gate_work(
    app_client: AsyncClient,
    fake_d1: Any,
    fake_env: Any,
    fixture_payload: dict[str, Any],
) -> None:
    _authorize(fake_d1, fixture_payload)
    oversized = b"x" * (262_144 + 1)

    response = await app_client.post(
        "/v1/game-event-chunks",
        content=oversized,
        headers={"Authorization": "Bearer opaque"},
    )

    assert response.status_code == 413
    assert response.json() == {"detail": "request body exceeds maximum size"}
    assert fake_env.operation_order == []


async def test_max_chunk_bytes_uses_configured_positive_integer(
    app_client: AsyncClient,
    fake_d1: Any,
    fake_env: Any,
    fixture_bytes: bytes,
    fixture_payload: dict[str, Any],
) -> None:
    _authorize(fake_d1, fixture_payload)
    fake_env.MAX_CHUNK_BYTES = str(len(fixture_bytes) - 1)

    response = await app_client.post(
        "/v1/game-event-chunks",
        content=fixture_bytes,
        headers={"Authorization": "Bearer opaque"},
    )

    assert response.status_code == 413
    assert fake_env.operation_order == []


async def test_max_chunk_bytes_cannot_exceed_contractual_hard_limit(
    app_client: AsyncClient,
    fake_d1: Any,
    fake_env: Any,
    fixture_payload: dict[str, Any],
) -> None:
    _authorize(fake_d1, fixture_payload)
    fake_env.MAX_CHUNK_BYTES = "1048576"

    response = await app_client.post(
        "/v1/game-event-chunks",
        content=b"x" * (262_144 + 1),
        headers={"Authorization": "Bearer opaque"},
    )

    assert response.status_code == 413
    assert response.json() == {"detail": "request body exceeds maximum size"}
    assert fake_env.operation_order == []


@pytest.mark.parametrize(
    "configured",
    ["invalid", "", "9" * 5_000, 0, -1, True, 1.5],
)
async def test_invalid_max_chunk_bytes_uses_safe_default(
    app_client: AsyncClient,
    fake_d1: Any,
    fake_env: Any,
    fixture_bytes: bytes,
    fixture_payload: dict[str, Any],
    configured: object,
) -> None:
    _authorize(fake_d1, fixture_payload)
    fake_env.MAX_CHUNK_BYTES = configured

    response = await app_client.post(
        "/v1/game-event-chunks",
        content=fixture_bytes,
        headers={"Authorization": "Bearer opaque"},
    )

    assert response.status_code == 202


async def test_matching_preexisting_r2_object_is_reused_then_pointer_is_queued(
    app_client: AsyncClient,
    fake_d1: Any,
    fake_env: Any,
    fixture_bytes: bytes,
    fixture_payload: dict[str, Any],
) -> None:
    _authorize(fake_d1, fixture_payload)
    digest = hashlib.sha256(fixture_bytes).hexdigest()
    fake_env.RAW_BUCKET.seed_metadata(_raw_key(fixture_payload), digest)

    response = await app_client.post(
        "/v1/game-event-chunks",
        content=fixture_bytes,
        headers={"Authorization": "Bearer opaque"},
    )

    assert response.status_code == 202
    assert fake_env.RAW_BUCKET.put_calls == []
    assert len(fake_env.GAMEPLAY_QUEUE.send_calls) == 1
    assert fake_env.operation_order == [
        "gate.reserve",
        "queue.send",
        "gate.accept",
    ]


async def test_chunk_hash_conflict_returns_409(
    app_client: AsyncClient,
    fake_d1: Any,
    fake_env: Any,
    fixture_bytes: bytes,
    fixture_payload: dict[str, Any],
) -> None:
    _authorize(fake_d1, fixture_payload)
    fake_env.CHUNK_GATE.reserve_result = {"status": "conflict"}

    response = await app_client.post(
        "/v1/game-event-chunks",
        content=fixture_bytes,
        headers={"Authorization": "Bearer opaque"},
    )

    assert response.status_code == 409
    assert response.json() == {
        "detail": "chunk ID already contains different bytes"
    }


async def test_active_publication_returns_retryable_503(
    app_client: AsyncClient,
    fake_d1: Any,
    fake_env: Any,
    fixture_bytes: bytes,
    fixture_payload: dict[str, Any],
) -> None:
    _authorize(fake_d1, fixture_payload)
    fake_env.CHUNK_GATE.reserve_result = {"status": "publishing"}

    response = await app_client.post(
        "/v1/game-event-chunks",
        content=fixture_bytes,
        headers={"Authorization": "Bearer opaque"},
    )

    assert response.status_code == 503
    assert response.json() == {
        "detail": "chunk is currently publishing; retry later"
    }


@pytest.mark.parametrize("failure_target", ["r2", "queue"])
async def test_storage_and_queue_failures_return_safe_503(
    app_client: AsyncClient,
    fake_d1: Any,
    fake_env: Any,
    fixture_bytes: bytes,
    fixture_payload: dict[str, Any],
    failure_target: str,
) -> None:
    _authorize(fake_d1, fixture_payload)
    if failure_target == "r2":
        fake_env.RAW_BUCKET.head_failure = RuntimeError("R2 unavailable")
    else:
        fake_env.GAMEPLAY_QUEUE.failure = RuntimeError("Queue unavailable")

    response = await app_client.post(
        "/v1/game-event-chunks",
        content=fixture_bytes,
        headers={"Authorization": "Bearer opaque"},
    )

    assert response.status_code == 503
    assert response.json() == {"detail": "event ingestion unavailable"}
    assert len(fake_env.CHUNK_GATE.release_calls) == 1


class CountingStreamingRequest:
    def __init__(self, chunks: list[bytes], *, content_length: str | None = None) -> None:
        self.headers = (
            {} if content_length is None else {"content-length": content_length}
        )
        self.chunks = chunks
        self.read_count = 0

    async def stream(self) -> AsyncIterator[bytes]:
        for chunk in self.chunks:
            self.read_count += 1
            yield chunk


class ObservedBuffer:
    def __init__(self) -> None:
        self.data = bytearray()
        self.extend_calls: list[bytes] = []

    def __len__(self) -> int:
        return len(self.data)

    def __bytes__(self) -> bytes:
        return bytes(self.data)

    def extend(self, chunk: bytes) -> None:
        self.extend_calls.append(chunk)
        self.data.extend(chunk)


async def test_limited_reader_stops_immediately_after_crossing_limit() -> None:
    request = CountingStreamingRequest([b"123", b"456", b"must-not-be-read"])

    with pytest.raises(BodyTooLarge):
        await read_limited_body(
            cast(Request, cast(object, request)),
            max_bytes=5,
        )

    assert request.read_count == 2


async def test_limited_reader_does_not_copy_chunk_larger_than_remaining_capacity(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    observed = ObservedBuffer()
    request = CountingStreamingRequest([b"123", b"x" * 1_000_000])
    monkeypatch.setattr(
        request_body_module,
        "bytearray",
        lambda: observed,
        raising=False,
    )

    with pytest.raises(BodyTooLarge):
        await read_limited_body(
            cast(Request, cast(object, request)),
            max_bytes=5,
        )

    assert observed.extend_calls == [b"123"]


async def test_limited_reader_rejects_large_content_length_before_streaming() -> None:
    request = CountingStreamingRequest([b"must-not-be-read"], content_length="6")

    with pytest.raises(BodyTooLarge):
        await read_limited_body(
            cast(Request, cast(object, request)),
            max_bytes=5,
        )

    assert request.read_count == 0


async def test_limited_reader_rejects_extreme_content_length_without_parsing_it() -> None:
    request = CountingStreamingRequest(
        [b"must-not-be-read"],
        content_length="9" * 5_000,
    )

    with pytest.raises(BodyTooLarge):
        await read_limited_body(
            cast(Request, cast(object, request)),
            max_bytes=5,
        )

    assert request.read_count == 0
