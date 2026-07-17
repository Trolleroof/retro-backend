"""Transactional ingestion and idempotency service."""

from __future__ import annotations

import hashlib
import json
import time
from dataclasses import dataclass
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from ..contracts import validate_chunk
from ..models import GameplayEvent, GameSession, IngestedChunk, ProcessingJob, User


class ChunkConflictError(RuntimeError):
    pass


@dataclass(frozen=True)
class IngestionResult:
    chunk: dict[str, Any]
    duplicate: bool
    server_received_at_ms: int

    def acknowledgment(self) -> dict[str, Any]:
        return {
            "accepted": True,
            "duplicate": self.duplicate,
            "game_id": self.chunk["game_id"],
            "chunk_id": self.chunk["chunk_id"],
            "chunk_sequence": self.chunk["chunk_sequence"],
            "accepted_event_range": {
                "first": self.chunk["first_event_sequence"],
                "last": self.chunk["last_event_sequence"],
            },
            "server_received_at_ms": self.server_received_at_ms,
        }


async def ingest_chunk(session: AsyncSession, raw_payload: bytes) -> IngestionResult:
    chunk = validate_chunk(json.loads(raw_payload))
    digest = hashlib.sha256(raw_payload).hexdigest()
    existing = await session.get(IngestedChunk, chunk["chunk_id"])
    if existing is not None:
        if existing.payload_sha256 != digest or existing.raw_payload != raw_payload:
            raise ChunkConflictError("chunk ID already contains different bytes")
        return IngestionResult(
            chunk=chunk, duplicate=True, server_received_at_ms=int(time.time() * 1000)
        )

    sequence_owner = await session.scalar(
        select(IngestedChunk).where(
            IngestedChunk.game_id == chunk["game_id"],
            IngestedChunk.chunk_sequence == chunk["chunk_sequence"],
        )
    )
    if sequence_owner is not None:
        raise ChunkConflictError("chunk sequence already belongs to a different chunk")

    user_id = chunk.get("user_id", "usr_legacy")
    user = await session.get(User, user_id)
    if user is None:
        user = User(id=user_id)
        session.add(user)
        await session.flush([user])

    game = await session.get(GameSession, chunk["game_id"])
    if game is None:
        game = GameSession(
            id=chunk["game_id"],
            user_id=user_id,
            game_type=chunk["game_type"],
            game_version=chunk["game_version"],
            event_count=0,
            chunk_count=0,
        )
        session.add(game)
        await session.flush([game])
    elif (
        game.user_id != user_id
        or game.game_type != chunk["game_type"]
        or game.game_version != chunk["game_version"]
    ):
        raise ChunkConflictError("game identity does not match previously ingested chunks")

    stored = IngestedChunk(
        id=chunk["chunk_id"],
        game_id=chunk["game_id"],
        user_id=user_id,
        schema_version=chunk["schema_version"],
        chunk_sequence=chunk["chunk_sequence"],
        first_event_sequence=chunk["first_event_sequence"],
        last_event_sequence=chunk["last_event_sequence"],
        event_count=chunk["event_count"],
        created_at_ms=round(chunk["created_at_ms"]),
        payload_sha256=digest,
        raw_payload=raw_payload,
    )
    session.add(stored)
    await session.flush([stored])
    for event in chunk["events"]:
        session.add(
            GameplayEvent(
                id=event["event_id"],
                chunk_id=chunk["chunk_id"],
                game_id=chunk["game_id"],
                user_id=user_id,
                event_sequence=event["event_sequence"],
                event_type=event["event_type"],
                visibility=event.get("visibility", "policy"),
                client_time_ms=round(event["client_time_ms"]),
                monotonic_time_us=round(
                    event.get("monotonic_time_us", event.get("monotonic_time_ms", 0) * 1000)
                ),
                simulation_tick=event.get("simulation_tick"),
                payload=event["payload"],
            )
        )
    game.event_count += chunk["event_count"]
    game.chunk_count += 1
    session.add(
        ProcessingJob(
            id=f"process:{chunk['chunk_id']}",
            chunk_id=chunk["chunk_id"],
        )
    )
    await session.flush()
    return IngestionResult(
        chunk=chunk, duplicate=False, server_received_at_ms=int(time.time() * 1000)
    )
