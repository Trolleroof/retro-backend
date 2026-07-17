import json
from dataclasses import replace
from pathlib import Path

import pytest

from retro_backend.consumer.s3_writer import ObjectReceipt
from retro_backend.consumer.types import BrokerRecord, EventBatch, Partition
from retro_backend.consumer.worker import ConsumerWorker

FIXTURE = (
    Path(__file__).resolve().parents[2]
    / "contracts"
    / "fixtures"
    / "interaction-trace-chunk-v2.json"
)


def record(offset: int = 3, *, valid: bool = True) -> BrokerRecord:
    payload = json.loads(FIXTURE.read_text()) if valid else {"schema_version": 2}
    user_id = payload.get("user_id", "usr_1")
    return BrokerRecord(
        topic="retro-game-events",
        partition=1,
        offset=offset,
        timestamp_ms=1_700_000_000_000,
        key=b"game",
        value=json.dumps(payload).encode(),
        headers=(("retro-user-id", str(user_id).encode()),),
    )


class FakeCommitter:
    def __init__(self) -> None:
        self.commits: list[tuple[Partition, int]] = []

    async def commit(self, partition: Partition, next_offset: int) -> None:
        self.commits.append((partition, next_offset))


class FakeWriter:
    def __init__(self) -> None:
        self.batches: list[EventBatch] = []
        self.failure: Exception | None = None

    async def write(self, batch: EventBatch) -> ObjectReceipt:
        self.batches.append(batch)
        if self.failure:
            raise self.failure
        return ObjectReceipt("key", "digest", 1, False)


class FakeDlq:
    def __init__(self) -> None:
        self.records: list[tuple[BrokerRecord, str]] = []
        self.failure: Exception | None = None

    async def publish(self, value: BrokerRecord, reason: str) -> None:
        self.records.append((value, reason))
        if self.failure:
            raise self.failure


def worker(
    committer: FakeCommitter, writer: FakeWriter, dlq: FakeDlq, *, max_records: int = 1
) -> ConsumerWorker:
    return ConsumerWorker(
        committer=committer,
        batch_writer=writer,
        dlq=dlq,
        max_records=max_records,
        max_rows=100,
        max_bytes=1_000_000,
    )


@pytest.mark.asyncio
async def test_worker_commits_next_offset_only_after_parquet_write() -> None:
    committer, writer, dlq = FakeCommitter(), FakeWriter(), FakeDlq()

    await worker(committer, writer, dlq).handle(record(3))

    assert writer.batches[0].end_offset == 3
    assert committer.commits == [(Partition("retro-game-events", 1), 4)]
    assert not dlq.records


@pytest.mark.asyncio
async def test_worker_does_not_commit_when_parquet_write_fails() -> None:
    committer, writer, dlq = FakeCommitter(), FakeWriter(), FakeDlq()
    writer.failure = OSError("s3 unavailable")

    with pytest.raises(OSError, match="s3 unavailable"):
        await worker(committer, writer, dlq).handle(record(3))

    assert not committer.commits


@pytest.mark.asyncio
async def test_worker_dlq_ack_precedes_malformed_record_commit() -> None:
    committer, writer, dlq = FakeCommitter(), FakeWriter(), FakeDlq()

    await worker(committer, writer, dlq).handle(record(8, valid=False))

    assert dlq.records and "required" in dlq.records[0][1]
    assert committer.commits == [(Partition("retro-game-events", 1), 9)]
    assert not writer.batches


@pytest.mark.asyncio
async def test_worker_does_not_commit_when_dlq_publish_fails() -> None:
    committer, writer, dlq = FakeCommitter(), FakeWriter(), FakeDlq()
    dlq.failure = RuntimeError("broker unavailable")

    with pytest.raises(RuntimeError, match="broker unavailable"):
        await worker(committer, writer, dlq).handle(record(8, valid=False))

    assert not committer.commits


@pytest.mark.asyncio
async def test_worker_flushes_valid_prefix_before_dlq_record() -> None:
    committer, writer, dlq = FakeCommitter(), FakeWriter(), FakeDlq()
    value = worker(committer, writer, dlq, max_records=10)

    await value.handle(record(3))
    assert not committer.commits
    await value.handle(record(4, valid=False))

    assert [offset for _, offset in committer.commits] == [4, 5]
    assert writer.batches[0].end_offset == 3


@pytest.mark.asyncio
async def test_worker_splits_batches_at_utc_day_boundary() -> None:
    committer, writer, dlq = FakeCommitter(), FakeWriter(), FakeDlq()
    value = worker(committer, writer, dlq, max_records=10)

    await value.handle(replace(record(3), timestamp_ms=86_399_999))
    await value.handle(replace(record(4), timestamp_ms=86_400_000))
    await value.flush_all()

    assert [(batch.start_offset, batch.end_offset) for batch in writer.batches] == [(3, 3), (4, 4)]
    assert [offset for _, offset in committer.commits] == [4, 5]
