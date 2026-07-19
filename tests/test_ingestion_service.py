import hashlib
import json
import re
import tomllib
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
from pydantic import ValidationError

from retro_backend.contracts import (
    EventRange,
    GameplayPointer,
    IngestAcknowledgment,
)
from retro_backend.services.ingestion import (
    ChunkConflict,
    IngestionUnavailable,
    ingest_chunk,
)

NOW_MS = 1_784_241_600_000
ACCEPT_MS = NOW_MS + 5


def _ack_data(payload: dict[str, Any]) -> dict[str, object]:
    return {
        "accepted": True,
        "duplicate": False,
        "game_id": payload["game_id"],
        "chunk_id": payload["chunk_id"],
        "chunk_sequence": payload["chunk_sequence"],
        "accepted_event_range": {
            "first": payload["first_event_sequence"],
            "last": payload["last_event_sequence"],
        },
        "queued": True,
        "server_received_at_ms": NOW_MS,
    }


def test_pointer_and_acknowledgment_contracts_are_strict_and_immutable() -> None:
    pointer_data = {
        "version": 1,
        "r2_key": "raw/user=usr_01/game=game_01/chunk=chk_01.json",
        "payload_sha256": "a" * 64,
        "user_id": "usr_01",
        "game_id": "game_01",
        "chunk_id": "chk_01",
        "received_at_ms": 0,
    }
    pointer = GameplayPointer.model_validate(pointer_data)

    with pytest.raises(ValidationError, match="Instance is frozen"):
        pointer.r2_key = "changed"
    with pytest.raises(ValidationError, match="Extra inputs are not permitted"):
        GameplayPointer.model_validate({**pointer_data, "raw_body": "forbidden"})
    with pytest.raises(ValidationError):
        GameplayPointer.model_validate({**pointer_data, "payload_sha256": "A" * 64})

    acknowledgment = {
        "accepted": True,
        "duplicate": False,
        "game_id": "game_01",
        "chunk_id": "chk_01",
        "chunk_sequence": 1,
        "accepted_event_range": {"first": 1, "last": 4},
        "queued": True,
        "server_received_at_ms": 0,
    }
    with pytest.raises(ValidationError, match="Extra inputs are not permitted"):
        IngestAcknowledgment.model_validate(
            {**acknowledgment, "broker_partition": 0}
        )
    with pytest.raises(ValidationError):
        EventRange(first=0, last=1)


async def test_first_publish_stores_exact_bytes_before_pointer_queue(
    fake_env: Any,
    fixture_bytes: bytes,
    fixture_payload: dict[str, Any],
) -> None:
    digest = hashlib.sha256(fixture_bytes).hexdigest()

    result = await ingest_chunk(
        env=fake_env,
        user_id=fixture_payload["user_id"],
        raw_body=fixture_bytes,
        now_ms=NOW_MS,
        clock_ms=lambda: ACCEPT_MS,
    )

    expected_key = (
        f"raw/user={fixture_payload['user_id']}/game={fixture_payload['game_id']}"
        f"/chunk={fixture_payload['chunk_id']}.json"
    )
    assert fake_env.operation_order == [
        "gate.reserve",
        "r2.put",
        "queue.send",
        "gate.accept",
    ]
    assert fake_env.RAW_BUCKET.put_calls[0].body is fixture_bytes
    assert fake_env.GAMEPLAY_QUEUE.send_calls == [
        {
            "version": 1,
            "r2_key": expected_key,
            "payload_sha256": digest,
            "user_id": fixture_payload["user_id"],
            "game_id": fixture_payload["game_id"],
            "chunk_id": fixture_payload["chunk_id"],
            "received_at_ms": NOW_MS,
        }
    ]
    assert len(json.dumps(fake_env.GAMEPLAY_QUEUE.send_calls[0])) < 1_024
    assert fixture_bytes not in fake_env.GAMEPLAY_QUEUE.send_calls
    assert result.model_dump(mode="json") == _ack_data(fixture_payload)
    reserve_call = fake_env.CHUNK_GATE.stub.reserve_calls[0]
    assert reserve_call[0] == digest
    assert re.fullmatch(r"[0-9a-f]{64}", reserve_call[1])
    assert reserve_call[2:] == (NOW_MS, 30_000)
    lease_token = reserve_call[1]
    assert fake_env.CHUNK_GATE.stub.accept_calls == [
        (
            digest,
            lease_token,
            _ack_data(fixture_payload),
            ACCEPT_MS,
            172_800_000,
        )
    ]


async def test_accept_uses_fresh_time_without_changing_request_receipt_time(
    fake_env: Any,
    fixture_bytes: bytes,
    fixture_payload: dict[str, Any],
) -> None:
    accepted_at_ms = NOW_MS + 29_999

    result = await ingest_chunk(
        fake_env,
        fixture_payload["user_id"],
        fixture_bytes,
        NOW_MS,
        clock_ms=lambda: accepted_at_ms,
    )

    assert result.server_received_at_ms == NOW_MS
    assert fake_env.CHUNK_GATE.stub.accept_calls[0][-2] == accepted_at_ms


async def test_duplicate_returns_validated_cached_ack_without_r2_or_queue(
    fake_env: Any,
    fixture_bytes: bytes,
    fixture_payload: dict[str, Any],
) -> None:
    fake_env.CHUNK_GATE.reserve_result = {
        "status": "duplicate",
        "ack": _ack_data(fixture_payload),
    }

    result = await ingest_chunk(
        fake_env,
        fixture_payload["user_id"],
        fixture_bytes,
        NOW_MS + 1,
    )

    assert result.duplicate is True
    assert result.server_received_at_ms == NOW_MS
    assert fake_env.operation_order == ["gate.reserve"]
    assert fake_env.RAW_BUCKET.head_calls == []
    assert fake_env.RAW_BUCKET.put_calls == []
    assert fake_env.GAMEPLAY_QUEUE.send_calls == []


async def test_gate_conflict_is_deterministic_and_does_not_release_foreign_lease(
    fake_env: Any,
    fixture_bytes: bytes,
    fixture_payload: dict[str, Any],
) -> None:
    fake_env.CHUNK_GATE.reserve_result = {"status": "conflict"}

    with pytest.raises(
        ChunkConflict,
        match="chunk ID already contains different bytes",
    ):
        await ingest_chunk(
            fake_env,
            fixture_payload["user_id"],
            fixture_bytes,
            NOW_MS,
        )

    assert fake_env.operation_order == ["gate.reserve"]
    assert fake_env.CHUNK_GATE.release_calls == []


async def test_active_publication_is_retryable_without_r2_or_queue(
    fake_env: Any,
    fixture_bytes: bytes,
    fixture_payload: dict[str, Any],
) -> None:
    fake_env.CHUNK_GATE.reserve_result = {"status": "publishing"}

    with pytest.raises(
        IngestionUnavailable,
        match="chunk is currently publishing; retry later",
    ):
        await ingest_chunk(
            fake_env,
            fixture_payload["user_id"],
            fixture_bytes,
            NOW_MS,
        )

    assert fake_env.operation_order == ["gate.reserve"]
    assert fake_env.RAW_BUCKET.head_calls == []
    assert fake_env.GAMEPLAY_QUEUE.send_calls == []


async def test_r2_failure_releases_gate_and_returns_retryable_error(
    fake_env: Any,
    fixture_bytes: bytes,
    fixture_payload: dict[str, Any],
) -> None:
    fake_env.RAW_BUCKET.head_failure = RuntimeError("R2 unavailable")

    with pytest.raises(IngestionUnavailable, match="event ingestion unavailable"):
        await ingest_chunk(
            fake_env,
            fixture_payload["user_id"],
            fixture_bytes,
            NOW_MS,
        )

    assert fake_env.operation_order == ["gate.reserve", "gate.release"]
    digest = hashlib.sha256(fixture_bytes).hexdigest()
    lease_token = fake_env.CHUNK_GATE.stub.reserve_calls[0][1]
    assert fake_env.CHUNK_GATE.release_calls == [(digest, lease_token)]
    assert fake_env.GAMEPLAY_QUEUE.send_calls == []


async def test_queue_failure_releases_gate_and_returns_retryable_error(
    fake_env: Any,
    fixture_bytes: bytes,
    fixture_payload: dict[str, Any],
) -> None:
    fake_env.GAMEPLAY_QUEUE.failure = RuntimeError("Queue unavailable")

    with pytest.raises(IngestionUnavailable, match="event ingestion unavailable"):
        await ingest_chunk(
            fake_env,
            fixture_payload["user_id"],
            fixture_bytes,
            NOW_MS,
        )

    assert fake_env.operation_order == [
        "gate.reserve",
        "r2.put",
        "queue.send",
        "gate.release",
    ]


async def test_gate_accept_failure_releases_gate_and_returns_retryable_error(
    fake_env: Any,
    fixture_bytes: bytes,
    fixture_payload: dict[str, Any],
) -> None:
    fake_env.CHUNK_GATE.stub.accept_failure = RuntimeError("DO unavailable")

    with pytest.raises(IngestionUnavailable, match="event ingestion unavailable"):
        await ingest_chunk(
            fake_env,
            fixture_payload["user_id"],
            fixture_bytes,
            NOW_MS,
        )

    assert fake_env.operation_order == [
        "gate.reserve",
        "r2.put",
        "queue.send",
        "gate.accept",
        "gate.release",
    ]


async def test_r2_hash_conflict_releases_gate_and_stays_deterministic(
    fake_env: Any,
    fixture_bytes: bytes,
    fixture_payload: dict[str, Any],
) -> None:
    key = (
        f"raw/user={fixture_payload['user_id']}/game={fixture_payload['game_id']}"
        f"/chunk={fixture_payload['chunk_id']}.json"
    )
    fake_env.RAW_BUCKET.seed_metadata(key, "0" * 64)

    with pytest.raises(
        ChunkConflict,
        match="chunk ID already contains different bytes",
    ):
        await ingest_chunk(
            fake_env,
            fixture_payload["user_id"],
            fixture_bytes,
            NOW_MS,
        )

    assert fake_env.operation_order == ["gate.reserve", "gate.release"]
    assert fake_env.GAMEPLAY_QUEUE.send_calls == []


async def test_release_failure_does_not_mask_original_queue_error(
    fake_env: Any,
    fixture_bytes: bytes,
    fixture_payload: dict[str, Any],
) -> None:
    queue_error = RuntimeError("Queue unavailable")
    fake_env.GAMEPLAY_QUEUE.failure = queue_error
    fake_env.CHUNK_GATE.stub.release_failure = RuntimeError("DO unavailable")

    with pytest.raises(IngestionUnavailable) as raised:
        await ingest_chunk(
            fake_env,
            fixture_payload["user_id"],
            fixture_bytes,
            NOW_MS,
        )

    assert raised.value.__cause__ is queue_error
    assert fake_env.operation_order[-1] == "gate.release"


async def test_reservation_failure_is_returned_as_safe_unavailable(
    fake_env: Any,
    fixture_bytes: bytes,
    fixture_payload: dict[str, Any],
) -> None:
    reserve_error = RuntimeError("internal proxy detail")
    fake_env.CHUNK_GATE.stub.reserve_failure = reserve_error

    with pytest.raises(IngestionUnavailable, match="event ingestion unavailable") as raised:
        await ingest_chunk(
            fake_env,
            fixture_payload["user_id"],
            fixture_bytes,
            NOW_MS,
        )

    assert raised.value.__cause__ is reserve_error
    assert fake_env.CHUNK_GATE.release_calls == []


async def test_invalid_cached_ack_is_returned_as_safe_unavailable(
    fake_env: Any,
    fixture_bytes: bytes,
    fixture_payload: dict[str, Any],
) -> None:
    fake_env.CHUNK_GATE.reserve_result = {
        "status": "duplicate",
        "ack": {"accepted": True},
    }

    with pytest.raises(IngestionUnavailable, match="event ingestion unavailable"):
        await ingest_chunk(
            fake_env,
            fixture_payload["user_id"],
            fixture_bytes,
            NOW_MS,
        )

    assert fake_env.RAW_BUCKET.head_calls == []
    assert fake_env.GAMEPLAY_QUEUE.send_calls == []


async def test_mapping_env_uses_explicit_duration_configuration(
    fake_env: Any,
    fixture_bytes: bytes,
    fixture_payload: dict[str, Any],
) -> None:
    environment = {
        "RAW_BUCKET": fake_env.RAW_BUCKET,
        "GAMEPLAY_QUEUE": fake_env.GAMEPLAY_QUEUE,
        "CHUNK_GATE": fake_env.CHUNK_GATE,
        "CHUNK_PUBLISHING_LEASE_MS": "777",
        "CHUNK_DEDUP_TTL_MS": 888,
    }

    await ingest_chunk(
        environment,
        fixture_payload["user_id"],
        fixture_bytes,
        NOW_MS,
    )

    digest = hashlib.sha256(fixture_bytes).hexdigest()
    reserve_call = fake_env.CHUNK_GATE.stub.reserve_calls[0]
    assert reserve_call[0] == digest
    assert reserve_call[2:] == (NOW_MS, 777)
    assert fake_env.CHUNK_GATE.stub.accept_calls[0][-1] == 888


@pytest.mark.parametrize(
    ("lease_value", "ttl_value"),
    [
        ("invalid", ""),
        ("9" * 5_000, "8" * 5_000),
        (0, -1),
        (True, False),
        (1.5, 2.5),
    ],
)
async def test_invalid_duration_configuration_uses_safe_defaults(
    fake_env: Any,
    fixture_bytes: bytes,
    fixture_payload: dict[str, Any],
    lease_value: object,
    ttl_value: object,
) -> None:
    environment = SimpleNamespace(
        RAW_BUCKET=fake_env.RAW_BUCKET,
        GAMEPLAY_QUEUE=fake_env.GAMEPLAY_QUEUE,
        CHUNK_GATE=fake_env.CHUNK_GATE,
        CHUNK_PUBLISHING_LEASE_MS=lease_value,
        CHUNK_DEDUP_TTL_MS=ttl_value,
    )

    await ingest_chunk(
        environment,
        fixture_payload["user_id"],
        fixture_bytes,
        NOW_MS,
    )

    digest = hashlib.sha256(fixture_bytes).hexdigest()
    reserve_call = fake_env.CHUNK_GATE.stub.reserve_calls[0]
    assert reserve_call[0] == digest
    assert reserve_call[2:] == (NOW_MS, 30_000)
    assert fake_env.CHUNK_GATE.stub.accept_calls[0][-1] == 172_800_000


async def test_missing_required_binding_is_safe_unavailable(
    fixture_bytes: bytes,
    fixture_payload: dict[str, Any],
) -> None:
    with pytest.raises(IngestionUnavailable, match="event ingestion unavailable"):
        await ingest_chunk(
            {},
            fixture_payload["user_id"],
            fixture_bytes,
            NOW_MS,
        )


def test_all_runtime_surfaces_are_checked_by_basedpyright() -> None:
    root = Path(__file__).resolve().parents[1]
    config = tomllib.loads((root / "pyproject.toml").read_text())

    assert "exclude" not in config["tool"]["basedpyright"]
