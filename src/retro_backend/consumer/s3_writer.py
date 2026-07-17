"""Idempotent object storage and Parquet batch persistence."""

from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Protocol

from .parquet import encode_rows
from .types import EventBatch


class ObjectConflictError(RuntimeError):
    pass


@dataclass(frozen=True, slots=True)
class ObjectInfo:
    size: int
    metadata: dict[str, str]


@dataclass(frozen=True, slots=True)
class ObjectReceipt:
    key: str
    sha256: str
    size: int
    existed: bool


class ObjectStorage(Protocol):
    async def head(self, key: str) -> ObjectInfo | None: ...

    async def put(self, key: str, body: bytes, metadata: dict[str, str]) -> None: ...


class BatchWriter(Protocol):
    async def write(self, batch: EventBatch) -> ObjectReceipt: ...


class IdempotentObjectWriter:
    def __init__(self, storage: ObjectStorage) -> None:
        self._storage = storage

    async def write(
        self, key: str, body: bytes, metadata: dict[str, str] | None = None
    ) -> ObjectReceipt:
        digest = hashlib.sha256(body).hexdigest()
        full_metadata = {**(metadata or {}), "sha256": digest}
        existing = await self._storage.head(key)
        if existing is not None:
            if existing.size == len(body) and existing.metadata.get("sha256") == digest:
                return ObjectReceipt(key, digest, len(body), True)
            raise ObjectConflictError(f"object {key} already exists with different bytes")
        await self._storage.put(key, body, full_metadata)
        stored = await self._storage.head(key)
        if stored is None or stored.size != len(body) or stored.metadata.get("sha256") != digest:
            raise OSError(f"object {key} could not be verified after upload")
        return ObjectReceipt(key, digest, len(body), False)


class ParquetBatchWriter:
    def __init__(self, writer: IdempotentObjectWriter, *, prefix: str = "raw") -> None:
        self._writer = writer
        self._prefix = prefix.strip("/")

    async def write(self, batch: EventBatch) -> ObjectReceipt:
        if batch.record_count != 1 or batch.start_offset != batch.end_offset:
            raise ValueError("raw Parquet objects must contain exactly one Kafka record")
        if not batch.rows or any(row.broker_offset != batch.start_offset for row in batch.rows):
            raise ValueError("raw Parquet rows must all belong to the object's Kafka offset")
        topic = re.sub(r"[^A-Za-z0-9._-]", "_", batch.partition.topic)
        day = datetime.fromtimestamp(batch.rows[0].ingested_at_ms / 1000, tz=UTC).date()
        key = (
            f"{self._prefix}/date={day.isoformat()}/topic={topic}/"
            f"partition={batch.partition.partition:05d}/"
            f"offsets={batch.start_offset:020d}-{batch.end_offset:020d}.parquet"
        )
        return await self._writer.write(
            key,
            encode_rows(batch.rows),
            {
                "topic": batch.partition.topic,
                "partition": str(batch.partition.partition),
                "start-offset": str(batch.start_offset),
                "end-offset": str(batch.end_offset),
            },
        )
