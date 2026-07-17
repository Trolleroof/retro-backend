"""R2-first gameplay ingestion coordinated by one per-chunk Durable Object."""

import hashlib
from contextlib import suppress
from typing import cast

from ..bindings import (
    DurableObjectNamespace,
    R2Bucket,
    is_binding_null,
    positive_int_field,
    value_field,
)
from ..chunk_gate import ChunkGateClient
from ..contracts import (
    EventRange,
    GameplayPointer,
    IngestAcknowledgment,
    parse_chunk_envelope,
)
from ..storage.queues import GameplayQueuePublisher
from ..storage.r2 import ObjectConflict, R2Store

DEFAULT_CHUNK_DEDUP_TTL_MS = 86_400_000
DEFAULT_CHUNK_PUBLISHING_LEASE_MS = 30_000


class ChunkConflict(RuntimeError):
    """Raised when a chunk identity is already bound to different bytes."""


class IngestionUnavailable(RuntimeError):
    """Raised for retryable ingest infrastructure failures."""


def _build_adapters(
    environment: object,
) -> tuple[R2Store, GameplayQueuePublisher, ChunkGateClient]:
    bucket = value_field(environment, "RAW_BUCKET")
    namespace = value_field(environment, "CHUNK_GATE")
    queue = value_field(environment, "GAMEPLAY_QUEUE")
    if any(is_binding_null(binding) for binding in (bucket, namespace, queue)):
        raise RuntimeError("required ingest binding unavailable")

    lease_ms = positive_int_field(
        environment,
        "CHUNK_PUBLISHING_LEASE_MS",
        DEFAULT_CHUNK_PUBLISHING_LEASE_MS,
    )
    dedupe_ttl_ms = positive_int_field(
        environment,
        "CHUNK_DEDUP_TTL_MS",
        DEFAULT_CHUNK_DEDUP_TTL_MS,
    )
    return (
        R2Store(cast(R2Bucket, bucket)),
        GameplayQueuePublisher(environment),
        ChunkGateClient(
            cast(DurableObjectNamespace, namespace),
            lease_ms=lease_ms,
            dedupe_ttl_ms=dedupe_ttl_ms,
        ),
    )


async def _release_without_masking(
    gate: ChunkGateClient,
    payload_sha256: str,
) -> None:
    with suppress(Exception):
        await gate.release(payload_sha256)


async def ingest_chunk(
    env: object,
    user_id: str,
    raw_body: bytes,
    now_ms: int,
) -> IngestAcknowledgment:
    envelope = parse_chunk_envelope(raw_body, user_id)
    payload_sha256 = hashlib.sha256(raw_body).hexdigest()

    try:
        store, queue, gate = _build_adapters(env)
        reservation = await gate.reserve(
            user_id,
            envelope.chunk_id,
            payload_sha256,
            now_ms,
        )
    except Exception as error:
        raise IngestionUnavailable("event ingestion unavailable") from error

    if reservation.status == "duplicate":
        if reservation.ack is None:
            raise IngestionUnavailable("event ingestion unavailable")
        return reservation.ack.model_copy(update={"duplicate": True})
    if reservation.status == "conflict":
        raise ChunkConflict("chunk ID already contains different bytes")
    if reservation.status == "publishing":
        raise IngestionUnavailable("chunk is currently publishing; retry later")

    try:
        r2_key = await store.put_raw(
            user_id=user_id,
            game_id=envelope.game_id,
            chunk_id=envelope.chunk_id,
            raw_body=raw_body,
            payload_sha256=payload_sha256,
        )
        pointer = GameplayPointer(
            r2_key=r2_key,
            payload_sha256=payload_sha256,
            user_id=user_id,
            game_id=envelope.game_id,
            chunk_id=envelope.chunk_id,
            received_at_ms=now_ms,
        )
        await queue.publish(pointer)
        acknowledgment = IngestAcknowledgment(
            duplicate=False,
            game_id=envelope.game_id,
            chunk_id=envelope.chunk_id,
            chunk_sequence=envelope.chunk_sequence,
            accepted_event_range=EventRange(
                first=envelope.first_event_sequence,
                last=envelope.last_event_sequence,
            ),
            server_received_at_ms=now_ms,
        )
        await gate.accept(payload_sha256, acknowledgment, now_ms)
        return acknowledgment
    except ObjectConflict as error:
        await _release_without_masking(gate, payload_sha256)
        raise ChunkConflict("chunk ID already contains different bytes") from error
    except ChunkConflict:
        await _release_without_masking(gate, payload_sha256)
        raise
    except Exception as error:
        await _release_without_masking(gate, payload_sha256)
        raise IngestionUnavailable("event ingestion unavailable") from error
