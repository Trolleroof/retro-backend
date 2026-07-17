import json
from typing import Any

from httpx import AsyncClient
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from retro_backend.models import GameplayEvent, GameSession, IngestedChunk, ProcessingJob


async def test_ingestion_requires_bearer_token(client: AsyncClient, fixture_bytes: bytes) -> None:
    response = await client.post("/v1/game-event-chunks", content=fixture_bytes)
    assert response.status_code == 401


async def test_ingestion_persists_exact_chunk_events_and_job(
    client: AsyncClient,
    auth_headers: dict[str, str],
    fixture_bytes: bytes,
    fixture_payload: dict[str, Any],
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
    }

    async with session_factory() as session:
        chunk = await session.get(IngestedChunk, fixture_payload["chunk_id"])
        assert chunk is not None
        assert chunk.raw_payload == fixture_bytes
        assert await session.scalar(select(func.count(GameplayEvent.id))) == len(
            fixture_payload["events"]
        )
        assert await session.scalar(select(func.count(ProcessingJob.id))) == 1
        game = await session.get(GameSession, fixture_payload["game_id"])
        assert game is not None
        assert game.user_id == fixture_payload["user_id"]


async def test_identical_retry_is_idempotent(
    client: AsyncClient,
    auth_headers: dict[str, str],
    fixture_bytes: bytes,
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    first = await client.post("/v1/game-event-chunks", content=fixture_bytes, headers=auth_headers)
    second = await client.post("/v1/game-event-chunks", content=fixture_bytes, headers=auth_headers)
    assert first.status_code == second.status_code == 202
    assert second.json()["duplicate"] is True
    async with session_factory() as session:
        assert await session.scalar(select(func.count(IngestedChunk.id))) == 1


async def test_conflicting_chunk_reuse_returns_409(
    client: AsyncClient,
    auth_headers: dict[str, str],
    fixture_bytes: bytes,
    fixture_payload: dict[str, Any],
) -> None:
    await client.post("/v1/game-event-chunks", content=fixture_bytes, headers=auth_headers)
    fixture_payload["created_at_ms"] += 1
    response = await client.post(
        "/v1/game-event-chunks",
        content=json.dumps(fixture_payload, separators=(",", ":")),
        headers=auth_headers,
    )
    assert response.status_code == 409
    assert response.json()["detail"] == "chunk ID already contains different bytes"


async def test_game_and_event_query_api(
    client: AsyncClient,
    auth_headers: dict[str, str],
    fixture_bytes: bytes,
    fixture_payload: dict[str, Any],
) -> None:
    await client.post("/v1/game-event-chunks", content=fixture_bytes, headers=auth_headers)
    game = await client.get(f"/v1/games/{fixture_payload['game_id']}", headers=auth_headers)
    events = await client.get(
        f"/v1/games/{fixture_payload['game_id']}/events", headers=auth_headers
    )
    assert game.status_code == 200
    assert game.json()["event_count"] == fixture_payload["event_count"]
    assert events.status_code == 200
    assert [event["event_id"] for event in events.json()["events"]] == [
        event["event_id"] for event in fixture_payload["events"]
    ]
