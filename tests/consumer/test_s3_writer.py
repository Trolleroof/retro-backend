import hashlib

import pytest

from retro_backend.consumer.s3_writer import (
    IdempotentObjectWriter,
    ObjectConflictError,
    ObjectInfo,
    ParquetBatchWriter,
)
from retro_backend.consumer.types import EventBatch, Partition
from tests.consumer.test_parquet import event_row


class FakeStorage:
    def __init__(self) -> None:
        self.objects: dict[str, tuple[bytes, dict[str, str]]] = {}
        self.puts = 0

    async def head(self, key: str) -> ObjectInfo | None:
        value = self.objects.get(key)
        if value is None:
            return None
        body, metadata = value
        return ObjectInfo(size=len(body), metadata=metadata)

    async def put(self, key: str, body: bytes, metadata: dict[str, str]) -> None:
        self.puts += 1
        self.objects[key] = (body, metadata)


def batch() -> EventBatch:
    return EventBatch(
        partition=Partition("retro-game-events", 2),
        start_offset=10,
        end_offset=10,
        record_count=1,
        rows=(event_row(broker_offset=10),),
    )


@pytest.mark.asyncio
async def test_parquet_batch_writer_uses_deterministic_offset_key_and_checksum() -> None:
    storage = FakeStorage()
    writer = ParquetBatchWriter(IdempotentObjectWriter(storage), prefix="raw")

    receipt = await writer.write(batch())

    assert receipt.key == (
        "raw/date=1970-01-01/topic=retro-game-events/partition=00002/"
        "offsets=00000000000000000010-00000000000000000010.parquet"
    )
    body, metadata = storage.objects[receipt.key]
    assert metadata["sha256"] == hashlib.sha256(body).hexdigest()
    assert metadata["start-offset"] == "10"
    assert metadata["end-offset"] == "10"


@pytest.mark.asyncio
async def test_identical_retry_head_checks_without_reuploading() -> None:
    storage = FakeStorage()
    writer = ParquetBatchWriter(IdempotentObjectWriter(storage), prefix="raw")

    first = await writer.write(batch())
    second = await writer.write(batch())

    assert (first.key, first.sha256, first.size) == (second.key, second.sha256, second.size)
    assert second.existed
    assert storage.puts == 1


@pytest.mark.asyncio
async def test_existing_different_object_is_a_conflict() -> None:
    storage = FakeStorage()
    writer = ParquetBatchWriter(IdempotentObjectWriter(storage), prefix="raw")
    receipt = await writer.write(batch())
    storage.objects[receipt.key] = (b"other", {"sha256": "wrong"})

    with pytest.raises(ObjectConflictError):
        await writer.write(batch())


@pytest.mark.asyncio
async def test_raw_writer_rejects_nondeterministic_multi_record_boundaries() -> None:
    storage = FakeStorage()
    writer = ParquetBatchWriter(IdempotentObjectWriter(storage), prefix="raw")
    invalid = EventBatch(
        partition=Partition("retro-game-events", 2),
        start_offset=10,
        end_offset=11,
        record_count=2,
        rows=(event_row(broker_offset=10), event_row(event_id="evt_2", broker_offset=11)),
    )

    with pytest.raises(ValueError, match="exactly one Kafka record"):
        await writer.write(invalid)

    assert not storage.objects
