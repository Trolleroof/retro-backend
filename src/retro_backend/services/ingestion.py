"""Lease-owned, Redis-deduplicated at-least-once event publication."""

from __future__ import annotations

import asyncio
import hashlib
import hmac
import json
import secrets
import time
from dataclasses import dataclass
from typing import Any

from redis.asyncio import Redis

from ..config import Settings
from ..producer import BrokerMetadata, EventProducer

COMPARE_DELETE_SCRIPT = """
if redis.call('GET', KEYS[1]) == ARGV[1] then
  return redis.call('DEL', KEYS[1])
end
return 0
"""

COMPARE_SET_SCRIPT = """
if redis.call('GET', KEYS[1]) == ARGV[1] then
  redis.call('SET', KEYS[1], ARGV[2], 'EX', ARGV[3])
  return 1
end
return 0
"""


class ChunkPublishingError(RuntimeError):
    pass


class ChunkConflictError(RuntimeError):
    pass


class BrokerPublishError(RuntimeError):
    pass


@dataclass(frozen=True)
class IngestionResult:
    duplicate: bool
    game_id: str
    chunk_id: str
    chunk_sequence: int
    first_event_sequence: int
    last_event_sequence: int
    server_received_at_ms: int
    broker_partition: int
    broker_offset: int

    def acknowledgment(self) -> dict[str, Any]:
        return {
            "accepted": True,
            "duplicate": self.duplicate,
            "game_id": self.game_id,
            "chunk_id": self.chunk_id,
            "chunk_sequence": self.chunk_sequence,
            "accepted_event_range": {
                "first": self.first_event_sequence,
                "last": self.last_event_sequence,
            },
            "server_received_at_ms": self.server_received_at_ms,
            "broker_partition": self.broker_partition,
            "broker_offset": self.broker_offset,
        }


def _serialize(value: dict[str, Any]) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"))


def _as_text(value: bytes | str | None) -> str | None:
    return value.decode("utf-8") if isinstance(value, bytes) else value


def _decode_state(value: str) -> dict[str, Any] | None:
    try:
        decoded = json.loads(value)
    except json.JSONDecodeError:
        return None
    return decoded if isinstance(decoded, dict) else None


def _result_from_receipt(state: dict[str, Any]) -> IngestionResult:
    ack = state.get("ack")
    if not isinstance(ack, dict):
        raise ChunkPublishingError("chunk acknowledgment is temporarily unavailable")
    try:
        return IngestionResult(
            duplicate=True,
            game_id=str(ack["game_id"]),
            chunk_id=str(ack["chunk_id"]),
            chunk_sequence=int(ack["chunk_sequence"]),
            first_event_sequence=int(ack["first_event_sequence"]),
            last_event_sequence=int(ack["last_event_sequence"]),
            server_received_at_ms=int(ack["server_received_at_ms"]),
            broker_partition=int(ack["broker_partition"]),
            broker_offset=int(ack["broker_offset"]),
        )
    except (KeyError, TypeError, ValueError) as error:
        raise ChunkPublishingError("chunk acknowledgment is temporarily unavailable") from error


def _new_result(envelope: dict[str, Any], metadata: BrokerMetadata) -> IngestionResult:
    return IngestionResult(
        duplicate=False,
        game_id=envelope["game_id"],
        chunk_id=envelope["chunk_id"],
        chunk_sequence=envelope["chunk_sequence"],
        first_event_sequence=envelope["first_event_sequence"],
        last_event_sequence=envelope["last_event_sequence"],
        server_received_at_ms=int(time.time() * 1000),
        broker_partition=metadata.partition,
        broker_offset=metadata.offset,
    )


def _receipt_state(result: IngestionResult, payload_sha256: str) -> str:
    return _serialize(
        {
            "version": 1,
            "state": "accepted",
            "payload_sha256": payload_sha256,
            "ack": {
                "game_id": result.game_id,
                "chunk_id": result.chunk_id,
                "chunk_sequence": result.chunk_sequence,
                "first_event_sequence": result.first_event_sequence,
                "last_event_sequence": result.last_event_sequence,
                "server_received_at_ms": result.server_received_at_ms,
                "broker_partition": result.broker_partition,
                "broker_offset": result.broker_offset,
            },
        }
    )


async def _delete_if_owner(redis: Redis, key: str, lease_state: str) -> None:
    await redis.eval(COMPARE_DELETE_SCRIPT, 1, key, lease_state)


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
    payload_sha256 = hashlib.sha256(raw_payload).hexdigest()
    lease_state = _serialize(
        {
            "version": 1,
            "state": "publishing",
            "lease_token": secrets.token_urlsafe(24),
            "payload_sha256": payload_sha256,
        }
    )
    acquired = await redis.set(
        dedup_key,
        lease_state,
        ex=settings.chunk_publishing_ttl_seconds,
        nx=True,
    )
    if not acquired:
        existing_value = _as_text(await redis.get(dedup_key))
        if existing_value is None or existing_value == "publishing":
            raise ChunkPublishingError("chunk is currently publishing; retry later")
        existing = _decode_state(existing_value)
        if existing is None:
            raise ChunkPublishingError("chunk acknowledgment is temporarily unavailable")
        existing_digest = existing.get("payload_sha256")
        if not isinstance(existing_digest, str) or not hmac.compare_digest(
            existing_digest, payload_sha256
        ):
            raise ChunkConflictError("chunk ID already contains different bytes")
        if existing.get("state") == "publishing":
            raise ChunkPublishingError("chunk is currently publishing; retry later")
        if existing.get("state") == "accepted":
            return _result_from_receipt(existing)
        raise ChunkPublishingError("chunk acknowledgment is temporarily unavailable")

    try:
        async with asyncio.timeout(settings.kafka_send_timeout_seconds):
            metadata = await producer.send(
                topic=settings.kafka_topic,
                value=raw_payload,
                key=envelope["game_id"].encode("utf-8"),
                headers=[
                    ("retro-user-id", user_id.encode("utf-8")),
                    ("retro-chunk-id", envelope["chunk_id"].encode("utf-8")),
                ],
            )
    except Exception as error:
        await _delete_if_owner(redis, dedup_key, lease_state)
        raise BrokerPublishError("event broker unavailable") from error

    result = _new_result(envelope, metadata)
    replaced = await redis.eval(
        COMPARE_SET_SCRIPT,
        1,
        dedup_key,
        lease_state,
        _receipt_state(result, payload_sha256),
        settings.chunk_dedup_ttl_seconds,
    )
    if replaced != 1:
        raise ChunkPublishingError("chunk publication ownership was lost; retry later")
    return result
