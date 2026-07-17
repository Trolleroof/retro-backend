import json
from pathlib import Path
from typing import Any, Protocol

from fakeredis.aioredis import FakeRedis
from httpx import AsyncClient
from jsonschema import Draft202012Validator
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from retro_backend.models import HighScore, User

ROOT = Path(__file__).resolve().parents[1]
INGEST_RESPONSE_SCHEMA = ROOT / "contracts/ingest-response-v1.schema.json"


class RecordingProducer(Protocol):
    calls: list[dict[str, Any]]
    failure: Exception | None


async def test_ingestion_requires_bearer_token(client: AsyncClient, fixture_bytes: bytes) -> None:
    response = await client.post("/v1/game-event-chunks", content=fixture_bytes)
    assert response.status_code == 401


async def test_runtime_acknowledgment_validates_against_response_schema(
    client: AsyncClient,
    auth_headers: dict[str, str],
    fixture_bytes: bytes,
) -> None:
    response = await client.post(
        "/v1/game-event-chunks", content=fixture_bytes, headers=auth_headers
    )

    assert response.status_code == 202
    schema = json.loads(INGEST_RESPONSE_SCHEMA.read_bytes())
    Draft202012Validator(schema).validate(response.json())


async def test_ingestion_publishes_exact_bytes_without_database_writes(
    client: AsyncClient,
    auth_headers: dict[str, str],
    fixture_bytes: bytes,
    fixture_payload: dict[str, Any],
    producer: RecordingProducer,
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    response = await client.post(
        "/v1/game-event-chunks", content=fixture_bytes, headers=auth_headers
    )

    assert response.status_code == 202
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
        "server_received_at_ms": response.json()["server_received_at_ms"],
        "broker_partition": 3,
        "broker_offset": 42,
    }
    assert producer.calls == [
        {
            "topic": "retro-game-events",
            "value": fixture_bytes,
            "key": fixture_payload["game_id"].encode(),
            "headers": [("retro-user-id", fixture_payload["user_id"].encode())],
        }
    ]
    async with session_factory() as session:
        assert await session.scalar(select(func.count(User.id))) == 1
        assert await session.scalar(select(func.count(HighScore.user_id))) == 0


async def test_duplicate_returns_cached_broker_ack_without_republishing(
    client: AsyncClient,
    auth_headers: dict[str, str],
    fixture_bytes: bytes,
    producer: RecordingProducer,
) -> None:
    first = await client.post("/v1/game-event-chunks", content=fixture_bytes, headers=auth_headers)
    second = await client.post("/v1/game-event-chunks", content=fixture_bytes, headers=auth_headers)

    assert first.status_code == second.status_code == 202
    assert second.json()["duplicate"] is True
    assert second.json()["broker_partition"] == 3
    assert second.json()["broker_offset"] == 42
    assert len(producer.calls) == 1


async def test_publishing_chunk_returns_retryable_503(
    client: AsyncClient,
    auth_headers: dict[str, str],
    fixture_bytes: bytes,
    fixture_payload: dict[str, Any],
    fake_redis: FakeRedis,
) -> None:
    key = f"retro:chunk:{fixture_payload['user_id']}:{fixture_payload['chunk_id']}"
    await fake_redis.set(key, "publishing", ex=30)

    response = await client.post(
        "/v1/game-event-chunks", content=fixture_bytes, headers=auth_headers
    )
    assert response.status_code == 503
    assert response.json()["detail"] == "chunk is currently publishing; retry later"


async def test_publish_failure_clears_dedup_key(
    client: AsyncClient,
    auth_headers: dict[str, str],
    fixture_bytes: bytes,
    fixture_payload: dict[str, Any],
    fake_redis: FakeRedis,
    producer: RecordingProducer,
) -> None:
    producer.failure = RuntimeError("broker unavailable")
    response = await client.post(
        "/v1/game-event-chunks", content=fixture_bytes, headers=auth_headers
    )

    assert response.status_code == 503
    assert response.json()["detail"] == "event broker unavailable"
    key = f"retro:chunk:{fixture_payload['user_id']}:{fixture_payload['chunk_id']}"
    assert await fake_redis.get(key) is None


async def test_ingestion_accepts_semantically_invalid_events_for_consumer_validation(
    client: AsyncClient,
    auth_headers: dict[str, str],
    fixture_payload: dict[str, Any],
    producer: RecordingProducer,
) -> None:
    fixture_payload["events"][0]["event_type"] = "consumer_will_reject_this"
    fixture_payload["events"][0]["payload"] = {"score": -999}
    raw = json.dumps(fixture_payload, separators=(",", ":")).encode()

    response = await client.post("/v1/game-event-chunks", content=raw, headers=auth_headers)
    assert response.status_code == 202
    assert producer.calls[0]["value"] == raw


async def test_ingestion_returns_clear_body_errors(
    client: AsyncClient,
    auth_headers: dict[str, str],
    fixture_payload: dict[str, Any],
) -> None:
    empty = await client.post("/v1/game-event-chunks", content=b"", headers=auth_headers)
    malformed = await client.post(
        "/v1/game-event-chunks", content=b"{", headers=auth_headers
    )
    fixture_payload.pop("chunk_id")
    missing = await client.post(
        "/v1/game-event-chunks",
        content=json.dumps(fixture_payload).encode(),
        headers=auth_headers,
    )

    assert empty.status_code == 400
    assert malformed.status_code == 400
    assert missing.status_code == 400


async def test_ingestion_rejects_oversized_body(
    client: AsyncClient,
    auth_headers: dict[str, str],
    fixture_payload: dict[str, Any],
) -> None:
    fixture_payload["padding"] = "x" * (257 * 1024)
    response = await client.post(
        "/v1/game-event-chunks",
        content=json.dumps(fixture_payload).encode(),
        headers=auth_headers,
    )
    assert response.status_code == 413
