import hashlib
from types import SimpleNamespace
from typing import Any, override

import pytest

from retro_backend.storage.r2 import ObjectConflict, R2Store


class JsNull:
    typeof = "object"

    @override
    def __repr__(self) -> str:
        return "jsnull"


def _raw_key(payload: dict[str, Any]) -> str:
    return (
        f"raw/user={payload['user_id']}/game={payload['game_id']}"
        f"/chunk={payload['chunk_id']}.json"
    )


async def test_put_raw_writes_exact_bytes_with_deterministic_metadata(
    fake_env: Any,
    fixture_bytes: bytes,
    fixture_payload: dict[str, Any],
) -> None:
    digest = hashlib.sha256(fixture_bytes).hexdigest()
    store = R2Store(fake_env.RAW_BUCKET)

    key = await store.put_raw(
        user_id=fixture_payload["user_id"],
        game_id=fixture_payload["game_id"],
        chunk_id=fixture_payload["chunk_id"],
        game_type=fixture_payload["game_type"],
        event_count=fixture_payload["event_count"],
        raw_body=fixture_bytes,
        payload_sha256=digest,
    )

    assert key == _raw_key(fixture_payload)
    assert fake_env.RAW_BUCKET.head_calls == [key]
    assert len(fake_env.RAW_BUCKET.put_calls) == 1
    put = fake_env.RAW_BUCKET.put_calls[0]
    assert put.key == key
    assert put.body is fixture_bytes
    assert put.options == {
        "onlyIf": {"etagDoesNotMatch": "*"},
        "sha256": digest,
        "httpMetadata": {"contentType": "application/json"},
        "customMetadata": {
            "payload_sha256": digest,
            "schema_version": "2",
            "user_id": fixture_payload["user_id"],
            "game_id": fixture_payload["game_id"],
            "chunk_id": fixture_payload["chunk_id"],
            "game_type": fixture_payload["game_type"],
            "event_count": str(fixture_payload["event_count"]),
        },
    }
    assert all(
        isinstance(value, str)
        for value in put.options["customMetadata"].values()
    )


async def test_put_raw_converts_body_and_options_before_crossing_r2_boundary(
    fake_env: Any,
    fixture_bytes: bytes,
    fixture_payload: dict[str, Any],
) -> None:
    digest = hashlib.sha256(fixture_bytes).hexdigest()
    converted_body = object()
    converted_options = {"converted": True}
    conversion_inputs: list[object] = []

    def to_js(value: object) -> object:
        conversion_inputs.append(value)
        return converted_body if value is fixture_bytes else converted_options

    fake_env.RAW_BUCKET.put_results.append(SimpleNamespace())
    store = R2Store(fake_env.RAW_BUCKET, to_js=to_js)

    await store.put_raw(
        user_id=fixture_payload["user_id"],
        game_id=fixture_payload["game_id"],
        chunk_id=fixture_payload["chunk_id"],
        game_type=fixture_payload["game_type"],
        event_count=fixture_payload["event_count"],
        raw_body=fixture_bytes,
        payload_sha256=digest,
    )

    assert conversion_inputs == [
        fixture_bytes,
        {
            "onlyIf": {"etagDoesNotMatch": "*"},
            "sha256": digest,
            "httpMetadata": {"contentType": "application/json"},
            "customMetadata": {
                "payload_sha256": digest,
                "schema_version": "2",
                "user_id": fixture_payload["user_id"],
                "game_id": fixture_payload["game_id"],
                "chunk_id": fixture_payload["chunk_id"],
                "game_type": fixture_payload["game_type"],
                "event_count": str(fixture_payload["event_count"]),
            },
        },
    ]
    put = fake_env.RAW_BUCKET.put_calls[0]
    assert put.body is converted_body
    assert put.body is not fixture_bytes
    assert put.options is converted_options


async def test_put_raw_reuses_existing_object_with_matching_proxy_metadata(
    fake_env: Any,
    fixture_bytes: bytes,
    fixture_payload: dict[str, Any],
) -> None:
    digest = hashlib.sha256(fixture_bytes).hexdigest()
    key = _raw_key(fixture_payload)
    fake_env.RAW_BUCKET.seed_metadata(key, digest, proxy=True)

    result = await R2Store(fake_env.RAW_BUCKET).put_raw(
        user_id=fixture_payload["user_id"],
        game_id=fixture_payload["game_id"],
        chunk_id=fixture_payload["chunk_id"],
        game_type=fixture_payload["game_type"],
        event_count=fixture_payload["event_count"],
        raw_body=fixture_bytes,
        payload_sha256=digest,
    )

    assert result == key
    assert fake_env.RAW_BUCKET.put_calls == []


@pytest.mark.parametrize("stored_digest", [None, "0" * 64])
async def test_put_raw_rejects_existing_object_without_matching_hash(
    fake_env: Any,
    fixture_bytes: bytes,
    fixture_payload: dict[str, Any],
    stored_digest: str | None,
) -> None:
    digest = hashlib.sha256(fixture_bytes).hexdigest()
    key = _raw_key(fixture_payload)
    fake_env.RAW_BUCKET.seed_metadata(key, stored_digest)

    with pytest.raises(ObjectConflict, match="raw object contains different bytes"):
        await R2Store(fake_env.RAW_BUCKET).put_raw(
            user_id=fixture_payload["user_id"],
            game_id=fixture_payload["game_id"],
            chunk_id=fixture_payload["chunk_id"],
            game_type=fixture_payload["game_type"],
            event_count=fixture_payload["event_count"],
            raw_body=fixture_bytes,
            payload_sha256=digest,
        )

    assert fake_env.RAW_BUCKET.put_calls == []


@pytest.mark.parametrize("conditional_result", [None, JsNull()])
async def test_put_raw_recovers_conditional_race_when_winner_hash_matches(
    fake_env: Any,
    fixture_bytes: bytes,
    fixture_payload: dict[str, Any],
    conditional_result: object | None,
) -> None:
    digest = hashlib.sha256(fixture_bytes).hexdigest()
    key = _raw_key(fixture_payload)
    winner = SimpleNamespace(customMetadata={"payload_sha256": digest})
    fake_env.RAW_BUCKET.head_results.extend([None, winner])
    fake_env.RAW_BUCKET.put_results.append(conditional_result)

    result = await R2Store(fake_env.RAW_BUCKET).put_raw(
        user_id=fixture_payload["user_id"],
        game_id=fixture_payload["game_id"],
        chunk_id=fixture_payload["chunk_id"],
        game_type=fixture_payload["game_type"],
        event_count=fixture_payload["event_count"],
        raw_body=fixture_bytes,
        payload_sha256=digest,
    )

    assert result == key
    assert fake_env.RAW_BUCKET.head_calls == [key, key]


async def test_put_raw_recovers_thrown_conditional_race_when_hash_matches(
    fake_env: Any,
    fixture_bytes: bytes,
    fixture_payload: dict[str, Any],
) -> None:
    digest = hashlib.sha256(fixture_bytes).hexdigest()
    key = _raw_key(fixture_payload)
    winner = SimpleNamespace(customMetadata={"payload_sha256": digest})
    fake_env.RAW_BUCKET.head_results.extend([None, winner])
    fake_env.RAW_BUCKET.put_failure = RuntimeError("conditional write failed")

    result = await R2Store(fake_env.RAW_BUCKET).put_raw(
        user_id=fixture_payload["user_id"],
        game_id=fixture_payload["game_id"],
        chunk_id=fixture_payload["chunk_id"],
        game_type=fixture_payload["game_type"],
        event_count=fixture_payload["event_count"],
        raw_body=fixture_bytes,
        payload_sha256=digest,
    )

    assert result == key
    assert fake_env.RAW_BUCKET.head_calls == [key, key]


async def test_put_raw_turns_conditional_race_hash_mismatch_into_conflict(
    fake_env: Any,
    fixture_bytes: bytes,
    fixture_payload: dict[str, Any],
) -> None:
    digest = hashlib.sha256(fixture_bytes).hexdigest()
    winner = SimpleNamespace(customMetadata={"payload_sha256": "0" * 64})
    fake_env.RAW_BUCKET.head_results.extend([None, winner])
    fake_env.RAW_BUCKET.put_results.append(None)

    with pytest.raises(ObjectConflict, match="raw object contains different bytes"):
        await R2Store(fake_env.RAW_BUCKET).put_raw(
            user_id=fixture_payload["user_id"],
            game_id=fixture_payload["game_id"],
            chunk_id=fixture_payload["chunk_id"],
            game_type=fixture_payload["game_type"],
            event_count=fixture_payload["event_count"],
            raw_body=fixture_bytes,
            payload_sha256=digest,
        )


async def test_put_raw_preserves_transient_put_error_when_no_race_winner_exists(
    fake_env: Any,
    fixture_bytes: bytes,
    fixture_payload: dict[str, Any],
) -> None:
    digest = hashlib.sha256(fixture_bytes).hexdigest()
    error = RuntimeError("R2 unavailable")
    fake_env.RAW_BUCKET.head_results.extend([None, None])
    fake_env.RAW_BUCKET.put_failure = error

    with pytest.raises(RuntimeError) as raised:
        await R2Store(fake_env.RAW_BUCKET).put_raw(
            user_id=fixture_payload["user_id"],
            game_id=fixture_payload["game_id"],
            chunk_id=fixture_payload["chunk_id"],
            game_type=fixture_payload["game_type"],
            event_count=fixture_payload["event_count"],
            raw_body=fixture_bytes,
            payload_sha256=digest,
        )

    assert raised.value is error


def test_raw_key_builder_accepts_maximum_id_suffixes_and_rejects_overlong() -> None:
    key = R2Store.raw_key(
        user_id=f"usr_{'u' * 120}",
        game_id=f"game_{'g' * 120}",
        chunk_id=f"chk_{'c' * 120}",
    )

    assert len(key.encode("utf-8")) <= 1024
    with pytest.raises(ValueError, match="user_id"):
        R2Store.raw_key(
            user_id=f"usr_{'u' * 121}",
            game_id="game_01",
            chunk_id="chk_01",
        )


async def test_derived_key_allows_1024_bytes_and_rejects_1025_before_binding(
    fake_env: Any,
) -> None:
    store = R2Store(fake_env.RAW_BUCKET)
    maximum_key = "k" * 1024

    result = await store.put_derived(maximum_key, b"{}")

    assert result == maximum_key
    head_calls = list(fake_env.RAW_BUCKET.head_calls)
    put_calls = list(fake_env.RAW_BUCKET.put_calls)
    with pytest.raises(ValueError, match="1024 bytes"):
        await store.put_derived("k" * 1025, b"{}")
    assert fake_env.RAW_BUCKET.head_calls == head_calls
    assert fake_env.RAW_BUCKET.put_calls == put_calls


@pytest.mark.parametrize(
    ("field", "value", "message"),
    [
        ("game_type", "tetris\nx-amz-meta-injected: 1", "game_type"),
        ("event_count", 0, "event_count"),
        ("event_count", True, "event_count"),
    ],
)
async def test_put_raw_rejects_header_unsafe_metadata_before_binding(
    fake_env: Any,
    fixture_bytes: bytes,
    fixture_payload: dict[str, Any],
    field: str,
    value: object,
    message: str,
) -> None:
    fixture_payload[field] = value

    with pytest.raises(ValueError, match=message):
        await R2Store(fake_env.RAW_BUCKET).put_raw(
            user_id=fixture_payload["user_id"],
            game_id=fixture_payload["game_id"],
            chunk_id=fixture_payload["chunk_id"],
            game_type=fixture_payload["game_type"],
            event_count=fixture_payload["event_count"],
            raw_body=fixture_bytes,
            payload_sha256=hashlib.sha256(fixture_bytes).hexdigest(),
        )

    assert fake_env.RAW_BUCKET.put_calls == []


async def test_raw_read_rejects_oversized_key_before_binding(fake_env: Any) -> None:
    with pytest.raises(ValueError, match="1024 bytes"):
        await R2Store(fake_env.RAW_BUCKET).get_raw("r" * 1025)

    assert fake_env.RAW_BUCKET.get_calls == []
