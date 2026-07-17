import hashlib
import json
from collections.abc import Callable
from datetime import UTC, datetime
from types import SimpleNamespace
from typing import Any, Protocol, cast

import pytest

from retro_backend.consumers.gameplay import GameplayConsumer, TransientGameplayError
from retro_backend.contracts import GameplayProcessResult

CREATED_DATE = "2026-07-16"
POINTER_RECEIVED_AT_MS = int(datetime(2026, 7, 17, 12, tzinfo=UTC).timestamp() * 1000)
MESSAGE_TIMESTAMP_MS = int(datetime(2026, 7, 18, 9, tzinfo=UTC).timestamp() * 1000)


class _BytesBuffer(Protocol):
    def to_bytes(self) -> bytes: ...


def _canonical(value: object) -> bytes:
    return json.dumps(
        value,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    ).encode("utf-8")


def _raw_key(payload: dict[str, Any]) -> str:
    return (
        f"raw/user={payload['user_id']}/game={payload['game_id']}"
        f"/chunk={payload['chunk_id']}.json"
    )


def _validated_key(payload: dict[str, Any]) -> str:
    return (
        f"validated/date={CREATED_DATE}/user={payload['user_id']}"
        f"/game={payload['game_id']}/chunk={payload['chunk_id']}.json"
    )


def _rejected_key(payload: dict[str, Any], date: str = CREATED_DATE) -> str:
    return (
        f"rejected/date={date}/user={payload['user_id']}"
        f"/game={payload['game_id']}/chunk={payload['chunk_id']}.json"
    )


def _pointer(
    identity: dict[str, Any],
    raw_body: bytes,
    *,
    received_at_ms: int = POINTER_RECEIVED_AT_MS,
) -> dict[str, object]:
    return {
        "version": 1,
        "r2_key": _raw_key(identity),
        "payload_sha256": hashlib.sha256(raw_body).hexdigest(),
        "user_id": identity["user_id"],
        "game_id": identity["game_id"],
        "chunk_id": identity["chunk_id"],
        "received_at_ms": received_at_ms,
    }


async def _process_payload(
    fake_env: Any,
    payload: dict[str, Any],
    *,
    identity: dict[str, Any] | None = None,
    received_at_ms: int = POINTER_RECEIVED_AT_MS,
) -> GameplayProcessResult:
    raw_body = _canonical(payload)
    pointer = _pointer(identity or payload, raw_body, received_at_ms=received_at_ms)
    fake_env.RAW_BUCKET.seed_raw(
        pointer["r2_key"],
        raw_body,
        pointer["payload_sha256"],
    )
    return await GameplayConsumer(fake_env).process(pointer)


async def test_valid_pointer_writes_exact_canonical_validated_artifact(
    fake_env: Any,
    fixture_bytes: bytes,
    fixture_payload: dict[str, Any],
) -> None:
    pointer = _pointer(fixture_payload, fixture_bytes)
    raw_object = fake_env.RAW_BUCKET.seed_raw(
        pointer["r2_key"],
        fixture_bytes,
        pointer["payload_sha256"],
        proxy_metadata=True,
        proxy_buffer=True,
    )
    conversion_inputs: list[object] = []

    def buffer_to_bytes(value: object) -> bytes:
        conversion_inputs.append(value)
        converted = cast(_BytesBuffer, value).to_bytes()
        assert isinstance(converted, bytes)
        return converted

    result = await GameplayConsumer(
        fake_env,
        buffer_to_bytes=buffer_to_bytes,
    ).process(pointer)

    assert result.status == "processed"
    assert result.reason_code is None
    assert result.artifact_key == _validated_key(fixture_payload)
    assert raw_object.array_buffer_calls == 1
    assert len(conversion_inputs) == 1

    put = fake_env.RAW_BUCKET.put_calls[-1]
    assert put.key == _validated_key(fixture_payload)
    assert isinstance(put.body, bytes)
    artifact = json.loads(put.body)
    assert artifact == {
        "artifact_version": 1,
        "authenticated_user_id": fixture_payload["user_id"],
        "chunk": fixture_payload,
        "payload_sha256": pointer["payload_sha256"],
        "pointer_received_at_ms": POINTER_RECEIVED_AT_MS,
    }
    assert put.body == _canonical(artifact)
    derived_digest = hashlib.sha256(put.body).hexdigest()
    assert put.options["customMetadata"] == {"payload_sha256": derived_digest}
    assert all(
        isinstance(value, str)
        for value in put.options["customMetadata"].values()
    )


async def test_validated_date_comes_from_chunk_created_at_not_processing_or_receipt_time(
    fake_env: Any,
    fixture_bytes: bytes,
    fixture_payload: dict[str, Any],
) -> None:
    pointer = _pointer(
        fixture_payload,
        fixture_bytes,
        received_at_ms=int(datetime(2035, 1, 1, tzinfo=UTC).timestamp() * 1000),
    )
    fake_env.RAW_BUCKET.seed_raw(
        pointer["r2_key"], fixture_bytes, pointer["payload_sha256"]
    )

    result = await GameplayConsumer(
        fake_env,
        clock_ms=lambda: int(datetime(2040, 1, 1, tzinfo=UTC).timestamp() * 1000),
    ).process(pointer)

    assert result.artifact_key.startswith(f"validated/date={CREATED_DATE}/")


async def test_matching_validated_artifact_is_deterministic_replay_success(
    fake_env: Any,
    fixture_bytes: bytes,
    fixture_payload: dict[str, Any],
) -> None:
    pointer = _pointer(fixture_payload, fixture_bytes)
    fake_env.RAW_BUCKET.seed_raw(
        pointer["r2_key"], fixture_bytes, pointer["payload_sha256"]
    )
    first = await GameplayConsumer(fake_env, clock_ms=lambda: 1).process(pointer)
    first_put = fake_env.RAW_BUCKET.put_calls[-1]

    second = await GameplayConsumer(fake_env, clock_ms=lambda: 999_999).process(pointer)

    assert second == first
    assert len(fake_env.RAW_BUCKET.put_calls) == 1
    assert isinstance(first_put.body, bytes)
    assert first_put.body == _canonical(json.loads(first_put.body))


async def test_validated_object_hash_conflict_becomes_persisted_permanent_rejection(
    fake_env: Any,
    fixture_bytes: bytes,
    fixture_payload: dict[str, Any],
) -> None:
    pointer = _pointer(fixture_payload, fixture_bytes)
    fake_env.RAW_BUCKET.seed_raw(
        pointer["r2_key"], fixture_bytes, pointer["payload_sha256"]
    )
    fake_env.RAW_BUCKET.seed_metadata(_validated_key(fixture_payload), "0" * 64)

    result = await GameplayConsumer(fake_env).process(pointer)

    assert result.status == "rejected"
    assert result.reason_code == "derived_object_conflict"
    assert result.artifact_key == _rejected_key(fixture_payload)
    rejection = json.loads(fake_env.RAW_BUCKET.put_calls[-1].body)
    assert rejection["reason_code"] == "derived_object_conflict"
    assert rejection["rejection_at_ms"] == POINTER_RECEIVED_AT_MS


async def test_missing_raw_object_is_transient(
    fake_env: Any,
    fixture_bytes: bytes,
    fixture_payload: dict[str, Any],
) -> None:
    pointer = _pointer(fixture_payload, fixture_bytes)

    with pytest.raises(TransientGameplayError, match="raw object unavailable"):
        await GameplayConsumer(fake_env).process(pointer)

    assert fake_env.RAW_BUCKET.put_calls == []


async def test_r2_get_failure_is_transient(
    fake_env: Any,
    fixture_bytes: bytes,
    fixture_payload: dict[str, Any],
) -> None:
    pointer = _pointer(fixture_payload, fixture_bytes)
    fake_env.RAW_BUCKET.get_failure = RuntimeError("private R2 detail")

    with pytest.raises(TransientGameplayError, match="raw object unavailable") as raised:
        await GameplayConsumer(fake_env).process(pointer)

    assert "private R2 detail" not in str(raised.value)


async def test_raw_custom_metadata_hash_mismatch_is_persisted_rejection(
    fake_env: Any,
    fixture_bytes: bytes,
    fixture_payload: dict[str, Any],
) -> None:
    pointer = _pointer(fixture_payload, fixture_bytes)
    fake_env.RAW_BUCKET.seed_raw(pointer["r2_key"], fixture_bytes, "0" * 64)

    result = await GameplayConsumer(fake_env).process(pointer)

    assert result.status == "rejected"
    assert result.reason_code == "raw_metadata_hash_mismatch"
    assert result.artifact_key == _rejected_key(fixture_payload)
    rejection = json.loads(fake_env.RAW_BUCKET.put_calls[-1].body)
    assert rejection["metadata_payload_sha256"] == "0" * 64


async def test_recomputed_raw_hash_mismatch_is_persisted_rejection(
    fake_env: Any,
    fixture_bytes: bytes,
    fixture_payload: dict[str, Any],
) -> None:
    pointer = _pointer(fixture_payload, fixture_bytes)
    changed_bytes = fixture_bytes + b" "
    fake_env.RAW_BUCKET.seed_raw(
        pointer["r2_key"], changed_bytes, pointer["payload_sha256"]
    )

    result = await GameplayConsumer(fake_env).process(pointer)

    assert result.status == "rejected"
    assert result.reason_code == "raw_payload_hash_mismatch"
    rejection = json.loads(fake_env.RAW_BUCKET.put_calls[-1].body)
    assert rejection["payload_sha256"] == pointer["payload_sha256"]
    assert rejection["observed_payload_sha256"] == hashlib.sha256(
        changed_bytes
    ).hexdigest()


async def test_invalid_pointer_uses_stable_quarantine_key_and_safe_artifact(
    fake_env: Any,
) -> None:
    body = {
        "version": 1,
        "user_id": "invalid-user",
        "received_at_ms": "not-an-integer",
        "bearer_token": "bearer-secret",
        "raw_body": {"events": [{"payload": "trajectory-secret"}]},
    }
    consumer = GameplayConsumer(fake_env, clock_ms=lambda: 123_456_789)

    first = await consumer.process(
        body,
        message_id="msg/stable-01",
        message_timestamp_ms=MESSAGE_TIMESTAMP_MS,
    )
    first_put = fake_env.RAW_BUCKET.put_calls[-1]
    second = await consumer.process(
        dict(reversed(list(body.items()))),
        message_id="msg/stable-01",
        message_timestamp_ms=MESSAGE_TIMESTAMP_MS,
    )

    assert first == second
    assert first.status == "rejected"
    assert first.reason_code == "invalid_pointer"
    assert first.artifact_key.startswith("rejected/date=2026-07-18/quarantine=")
    assert first.artifact_key.endswith(".json")
    assert len(fake_env.RAW_BUCKET.put_calls) == 1
    assert isinstance(first_put.body, bytes)
    rejection_text = first_put.body.decode("utf-8")
    rejection = json.loads(rejection_text)
    assert rejection["rejection_at_ms"] == MESSAGE_TIMESTAMP_MS
    assert rejection["reason"] == "queue message is not a valid gameplay pointer"
    assert "bearer-secret" not in rejection_text
    assert "trajectory-secret" not in rejection_text
    assert "raw_body" not in rejection_text
    assert "events" not in rejection_text


async def test_invalid_pointer_without_stable_timestamp_uses_epoch_zero(
    fake_env: Any,
) -> None:
    result = await GameplayConsumer(fake_env, clock_ms=lambda: 999_999).process(
        {"invalid": True},
        message_id="msg_no_timestamp",
    )

    assert result.artifact_key.startswith("rejected/date=1970-01-01/quarantine=")
    rejection = json.loads(fake_env.RAW_BUCKET.put_calls[-1].body)
    assert rejection["rejection_at_ms"] == 0


async def test_invalid_pointer_without_message_id_uses_timestamp_to_avoid_collision(
    fake_env: Any,
) -> None:
    body = {"invalid": True}

    first = await GameplayConsumer(fake_env).process(
        body,
        message_timestamp_ms=MESSAGE_TIMESTAMP_MS,
    )
    second = await GameplayConsumer(fake_env).process(
        body,
        message_timestamp_ms=MESSAGE_TIMESTAMP_MS + 1,
    )

    assert first.artifact_key != second.artifact_key
    assert len(fake_env.RAW_BUCKET.put_calls) == 2


async def test_cyclic_invalid_pointer_body_is_quarantined_deterministically(
    fake_env: Any,
) -> None:
    body: dict[str, object] = {"invalid": True}
    body["cycle"] = body

    first = await GameplayConsumer(fake_env).process(
        body,
        message_id="msg_cycle",
        message_timestamp_ms=MESSAGE_TIMESTAMP_MS,
    )
    second = await GameplayConsumer(fake_env).process(
        body,
        message_id="msg_cycle",
        message_timestamp_ms=MESSAGE_TIMESTAMP_MS,
    )

    assert first == second
    assert first.reason_code == "invalid_pointer"
    assert len(fake_env.RAW_BUCKET.put_calls) == 1


async def test_invalid_raw_json_is_rejected_without_leaking_raw_bytes(
    fake_env: Any,
    fixture_payload: dict[str, Any],
) -> None:
    raw_body = b'{"events":[{"payload":"trajectory-secret"}]'
    pointer = _pointer(fixture_payload, raw_body)
    fake_env.RAW_BUCKET.seed_raw(
        pointer["r2_key"], raw_body, pointer["payload_sha256"]
    )

    result = await GameplayConsumer(fake_env).process(pointer)

    assert result.status == "rejected"
    assert result.reason_code == "invalid_chunk_json"
    assert result.artifact_key == _rejected_key(fixture_payload, date="2026-07-17")
    rejection_text = fake_env.RAW_BUCKET.put_calls[-1].body.decode("utf-8")
    assert "trajectory-secret" not in rejection_text
    assert '"payload":' not in rejection_text


@pytest.mark.parametrize("field", ["game_id", "chunk_id"])
async def test_pointer_and_envelope_identity_mismatch_is_rejected(
    fake_env: Any,
    fixture_payload: dict[str, Any],
    field: str,
) -> None:
    identity = dict(fixture_payload)
    replacement = "game_other" if field == "game_id" else "chk_other"
    fixture_payload[field] = replacement
    for event in fixture_payload["events"]:
        event[field] = replacement

    result = await _process_payload(fake_env, fixture_payload, identity=identity)

    assert result.status == "rejected"
    assert result.reason_code == "pointer_identity_mismatch"


async def test_authenticated_event_identity_mismatch_is_rejected(
    fake_env: Any,
    fixture_payload: dict[str, Any],
) -> None:
    fixture_payload["events"][2]["user_id"] = "usr_someone_else"

    result = await _process_payload(fake_env, fixture_payload)

    assert result.status == "rejected"
    assert result.reason_code == "authenticated_identity_mismatch"


def _event_value(field: str, value: object) -> Callable[[dict[str, Any]], None]:
    def mutate(payload: dict[str, Any]) -> None:
        payload["events"][1][field] = value

    return mutate


def _remove_event_field(field: str) -> Callable[[dict[str, Any]], None]:
    def mutate(payload: dict[str, Any]) -> None:
        del payload["events"][1][field]

    return mutate


def _event_not_object(payload: dict[str, Any]) -> None:
    payload["events"][1] = ["not", "an", "object"]


def _duplicate_sequence(payload: dict[str, Any]) -> None:
    payload["events"][2]["event_sequence"] = payload["events"][1]["event_sequence"]


def _count_mismatch(payload: dict[str, Any]) -> None:
    payload["event_count"] += 1


def _first_range_mismatch(payload: dict[str, Any]) -> None:
    payload["first_event_sequence"] += 1


def _last_range_mismatch(payload: dict[str, Any]) -> None:
    payload["last_event_sequence"] -= 1


def _first_event_type(payload: dict[str, Any]) -> None:
    payload["events"][0]["event_type"] = "observation"


def _start_game_type(payload: dict[str, Any]) -> None:
    payload["events"][0]["payload"]["game_type"] = "snake"


def _start_game_version(payload: dict[str, Any]) -> None:
    payload["events"][0]["payload"]["game_version"] = "different"


@pytest.mark.parametrize(
    "mutate",
    [
        pytest.param(_event_not_object, id="event-object"),
        pytest.param(_event_value("event_id", "invalid"), id="event-id"),
        pytest.param(_event_value("event_sequence", 0), id="positive-sequence"),
        pytest.param(_duplicate_sequence, id="strict-ordering"),
        pytest.param(_count_mismatch, id="event-count"),
        pytest.param(_first_range_mismatch, id="first-range"),
        pytest.param(_last_range_mismatch, id="last-range"),
        pytest.param(_event_value("event_type", "unknown"), id="supported-type"),
        pytest.param(_event_value("event_type", []), id="event-type-strict-type"),
        pytest.param(_event_value("visibility", "public"), id="visibility"),
        pytest.param(_event_value("visibility", []), id="visibility-strict-type"),
        pytest.param(_event_value("schema_version", 1), id="schema-version"),
        pytest.param(_event_value("event_version", 2), id="event-version"),
        pytest.param(_event_value("client_time_ms", -1), id="client-time-negative"),
        pytest.param(_event_value("client_time_ms", True), id="client-time-bool"),
        pytest.param(
            _event_value("monotonic_time_us", -1), id="monotonic-time-negative"
        ),
        pytest.param(
            _event_value("monotonic_time_us", True), id="monotonic-time-bool"
        ),
        pytest.param(_event_value("simulation_tick", -1), id="simulation-tick-negative"),
        pytest.param(_event_value("simulation_tick", True), id="simulation-tick-bool"),
        pytest.param(_event_value("payload", []), id="payload-mapping"),
        pytest.param(_event_value("extra", "forbidden"), id="strict-event-fields"),
        pytest.param(_remove_event_field("payload"), id="required-event-field"),
        pytest.param(_first_event_type, id="first-episode-started"),
        pytest.param(_start_game_type, id="start-game-type"),
        pytest.param(_start_game_version, id="start-game-version"),
    ],
)
async def test_invalid_v2_event_semantics_are_permanently_rejected(
    fake_env: Any,
    fixture_payload: dict[str, Any],
    mutate: Callable[[dict[str, Any]], None],
) -> None:
    mutate(fixture_payload)

    result = await _process_payload(fake_env, fixture_payload)

    assert result.status == "rejected"
    assert result.reason_code == "invalid_chunk_contract"
    assert result.artifact_key == _rejected_key(fixture_payload)


async def test_top_level_chunk_extensions_are_preserved_in_validated_artifact(
    fake_env: Any,
    fixture_payload: dict[str, Any],
) -> None:
    fixture_payload["extension"] = {"source": "future-client"}

    result = await _process_payload(fake_env, fixture_payload)

    assert result.status == "processed"
    artifact = json.loads(fake_env.RAW_BUCKET.put_calls[-1].body)
    assert artifact["chunk"]["extension"] == {"source": "future-client"}


async def test_unrepresentable_chunk_date_is_permanently_rejected(
    fake_env: Any,
    fixture_payload: dict[str, Any],
) -> None:
    fixture_payload["created_at_ms"] = 10**30

    result = await _process_payload(fake_env, fixture_payload)

    assert result.status == "rejected"
    assert result.reason_code == "invalid_chunk_contract"
    assert result.artifact_key == _rejected_key(fixture_payload, date="2026-07-17")


async def test_semantic_rejection_uses_safe_chunk_date_and_deterministic_replay(
    fake_env: Any,
    fixture_payload: dict[str, Any],
) -> None:
    fixture_payload["events"][1]["event_type"] = "unknown"
    raw_body = _canonical(fixture_payload)
    pointer = _pointer(fixture_payload, raw_body)
    fake_env.RAW_BUCKET.seed_raw(
        pointer["r2_key"], raw_body, pointer["payload_sha256"]
    )

    first = await GameplayConsumer(fake_env, clock_ms=lambda: 1).process(pointer)
    first_put = fake_env.RAW_BUCKET.put_calls[-1]
    second = await GameplayConsumer(fake_env, clock_ms=lambda: 2).process(pointer)

    assert first == second
    assert first.artifact_key == _rejected_key(fixture_payload)
    assert len(fake_env.RAW_BUCKET.put_calls) == 1
    assert isinstance(first_put.body, bytes)
    rejection_text = first_put.body.decode("utf-8")
    rejection = json.loads(rejection_text)
    assert rejection == {
        "artifact_version": 1,
        "chunk_id": fixture_payload["chunk_id"],
        "game_id": fixture_payload["game_id"],
        "payload_sha256": pointer["payload_sha256"],
        "pointer_received_at_ms": POINTER_RECEIVED_AT_MS,
        "reason": "chunk does not satisfy the v2 event contract",
        "reason_code": "invalid_chunk_contract",
        "rejection_at_ms": POINTER_RECEIVED_AT_MS,
        "user_id": fixture_payload["user_id"],
    }
    assert "events" not in rejection_text
    assert "trajectory" not in rejection_text


async def test_rejection_write_failure_is_transient(
    fake_env: Any,
    fixture_payload: dict[str, Any],
) -> None:
    fixture_payload["events"][1]["event_type"] = "unknown"
    raw_body = _canonical(fixture_payload)
    pointer = _pointer(fixture_payload, raw_body)
    fake_env.RAW_BUCKET.seed_raw(
        pointer["r2_key"], raw_body, pointer["payload_sha256"]
    )
    fake_env.RAW_BUCKET.put_failure = RuntimeError("R2 rejection write failed")

    with pytest.raises(TransientGameplayError, match="rejection artifact unavailable"):
        await GameplayConsumer(fake_env).process(pointer)


async def test_rejection_object_hash_conflict_is_transient(
    fake_env: Any,
    fixture_payload: dict[str, Any],
) -> None:
    fixture_payload["events"][1]["event_type"] = "unknown"
    raw_body = _canonical(fixture_payload)
    pointer = _pointer(fixture_payload, raw_body)
    fake_env.RAW_BUCKET.seed_raw(
        pointer["r2_key"], raw_body, pointer["payload_sha256"]
    )
    fake_env.RAW_BUCKET.seed_metadata(_rejected_key(fixture_payload), "0" * 64)

    with pytest.raises(TransientGameplayError, match="rejection artifact unavailable"):
        await GameplayConsumer(fake_env).process(pointer)


@pytest.mark.parametrize(
    "body_shape",
    ["mapping", "json-string", "json-bytes", "js-buffer", "proxy"],
)
async def test_queue_pointer_body_shapes_are_normalized_safely(
    fake_env: Any,
    fixture_bytes: bytes,
    fixture_payload: dict[str, Any],
    body_shape: str,
    array_buffer_factory: Any,
) -> None:
    pointer = _pointer(fixture_payload, fixture_bytes)
    fake_env.RAW_BUCKET.seed_raw(
        pointer["r2_key"], fixture_bytes, pointer["payload_sha256"]
    )
    bodies: dict[str, object] = {
        "mapping": pointer,
        "json-string": json.dumps(pointer),
        "json-bytes": json.dumps(pointer).encode("utf-8"),
        "js-buffer": array_buffer_factory(json.dumps(pointer).encode("utf-8")),
        "proxy": SimpleNamespace(**pointer),
    }

    result = await GameplayConsumer(fake_env).process(bodies[body_shape])

    assert result.status == "processed"


async def test_proxy_pointer_with_extra_field_is_quarantined_without_leakage(
    fake_env: Any,
    fixture_bytes: bytes,
    fixture_payload: dict[str, Any],
) -> None:
    pointer = _pointer(fixture_payload, fixture_bytes)
    fake_env.RAW_BUCKET.seed_raw(
        pointer["r2_key"], fixture_bytes, pointer["payload_sha256"]
    )
    proxy = SimpleNamespace(**pointer, raw_body="trajectory-secret")

    result = await GameplayConsumer(fake_env).process(
        proxy,
        message_id="msg_proxy_extra",
        message_timestamp_ms=MESSAGE_TIMESTAMP_MS,
    )

    assert result.status == "rejected"
    assert result.reason_code == "invalid_pointer"
    assert fake_env.RAW_BUCKET.get_calls == []
    rejection_text = fake_env.RAW_BUCKET.put_calls[-1].body.decode("utf-8")
    assert "trajectory-secret" not in rejection_text
    assert "raw_body" not in rejection_text
