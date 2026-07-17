"""Redis-deduplicated event publication."""

from __future__ import annotations

import time
from dataclasses import dataclass
from typing import Any

from redis.asyncio import Redis

from ..config import Settings
from ..producer import BrokerMetadata, EventProducer

PUBLISHING = "publishing"


class ChunkPublishingError(RuntimeError):
    pass


class BrokerPublishError(RuntimeError):
    pass


@dataclass(frozen=True)
class IngestionResult:
    envelope: dict[str, Any]
    duplicate: bool
    broker: BrokerMetadata
    server_received_at_ms: int

    def acknowledgment(self) -> dict[str, Any]:
        return {
            "accepted": True,
            "duplicate": self.duplicate,
            "game_id": self.envelope["game_id"],
            "chunk_id": self.envelope["chunk_id"],
            "chunk_sequence": self.envelope["chunk_sequence"],
            "accepted_event_range": {
                "first": self.envelope["first_event_sequence"],
                "last": self.envelope["last_event_sequence"],
            },
            "server_received_at_ms": self.server_received_at_ms,
            "broker_partition": self.broker.partition,
            "broker_offset": self.broker.offset,
        }


def _parse_broker_metadata(value: str) -> BrokerMetadata | None:
    try:
        partition, offset = value.split(":", maxsplit=1)
        return BrokerMetadata(partition=int(partition), offset=int(offset))
    except (TypeError, ValueError):
        return None


async def publish_chunk(
    *,
    redis: Redis,
    producer: EventProducer,
    settings: Settings,
    user_id: str,
    envelope: dict[str, Any],
    raw_payload: bytes,
) -> IngestionResult:
    dedup_key = f"retro:chunk:{user_id}:{envelope['chunk_id']}"
    acquired = await redis.set(
        dedup_key,
        PUBLISHING,
        ex=settings.chunk_publishing_ttl_seconds,
        nx=True,
    )
    if not acquired:
        existing = await redis.get(dedup_key)
        if isinstance(existing, bytes):
            existing = existing.decode("utf-8")
        if existing == PUBLISHING or existing is None:
            raise ChunkPublishingError("chunk is currently publishing; retry later")
        metadata = _parse_broker_metadata(existing)
        if metadata is None:
            raise ChunkPublishingError("chunk acknowledgment is temporarily unavailable")
        return IngestionResult(
            envelope=envelope,
            duplicate=True,
            broker=metadata,
            server_received_at_ms=int(time.time() * 1000),
        )

    try:
        metadata = await producer.send(
            topic=settings.kafka_topic,
            value=raw_payload,
            key=envelope["game_id"].encode("utf-8"),
            headers=[("retro-user-id", user_id.encode("utf-8"))],
        )
    except Exception as error:
        await redis.delete(dedup_key)
        raise BrokerPublishError("event broker unavailable") from error

    await redis.set(
        dedup_key,
        f"{metadata.partition}:{metadata.offset}",
        ex=settings.chunk_dedup_ttl_seconds,
    )
    return IngestionResult(
        envelope=envelope,
        duplicate=False,
        broker=metadata,
        server_received_at_ms=int(time.time() * 1000),
    )
