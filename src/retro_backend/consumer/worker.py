"""Generation-safe record persistence and acknowledgment ordering."""

from __future__ import annotations

import asyncio
from typing import Protocol

from .metrics import ConsumerMetrics
from .s3_writer import BatchWriter
from .types import BrokerRecord, EventBatch, Partition
from .validation import RecordValidationError, decode_v2_record


class PartitionNotOwnedError(RuntimeError):
    """A record arrived after the partition was revoked from this generation."""


class OffsetGapError(RuntimeError):
    """A partition delivered a noncontiguous offset within one assignment."""


class OffsetCommitter(Protocol):
    async def commit(self, partition: Partition, next_offset: int) -> None: ...


class DlqPublisher(Protocol):
    async def publish(self, value: BrokerRecord, reason: str) -> None: ...


class ConsumerWorker:
    def __init__(
        self,
        *,
        committer: OffsetCommitter,
        batch_writer: BatchWriter,
        dlq: DlqPublisher,
        metrics: ConsumerMetrics | None = None,
    ) -> None:
        self._committer = committer
        self._batch_writer = batch_writer
        self._dlq = dlq
        self._next_offsets: dict[Partition, int] = {}
        self._owned_partitions: set[Partition] = set()
        self._assignments_seen = False
        self._generation_lock = asyncio.Lock()
        self._metrics = metrics

    async def assign(self, partitions: set[Partition]) -> None:
        async with self._generation_lock:
            self._assignments_seen = True
            for partition in partitions:
                self._owned_partitions.add(partition)
                self._next_offsets.pop(partition, None)

    async def revoke(self, partitions: set[Partition]) -> None:
        # Waiting for this lock lets an already-started durable write and its commit
        # finish before ownership is detached. New records cannot enter concurrently.
        async with self._generation_lock:
            self._assignments_seen = True
            for partition in partitions:
                self._owned_partitions.discard(partition)
                self._next_offsets.pop(partition, None)

    def _assert_owned(self, partition: Partition) -> None:
        if self._assignments_seen and partition not in self._owned_partitions:
            raise PartitionNotOwnedError(f"partition {partition} is not owned by this generation")
        if not self._assignments_seen:
            self._owned_partitions.add(partition)

    def _assert_contiguous(self, record: BrokerRecord) -> None:
        expected = self._next_offsets.get(record.topic_partition)
        if expected is not None and record.offset != expected:
            raise OffsetGapError(f"expected offset {expected}, received {record.offset}")

    async def handle(self, record: BrokerRecord) -> None:
        async with self._generation_lock:
            partition = record.topic_partition
            self._assert_owned(partition)
            self._assert_contiguous(record)
            try:
                rows = decode_v2_record(record)
            except RecordValidationError as error:
                await self._dlq.publish(record, str(error))
                await self._committer.commit(partition, record.offset + 1)
                if self._metrics is not None:
                    self._metrics.records.labels(outcome="dlq").inc()
                    self._set_committed_metric(partition, record.offset + 1)
                    self._metrics.mark_progress()
                self._next_offsets[partition] = record.offset + 1
                return

            batch = EventBatch(
                partition=partition,
                start_offset=record.offset,
                end_offset=record.offset,
                record_count=1,
                rows=tuple(rows),
            )
            await self._batch_writer.write(batch)
            if self._metrics is not None:
                self._metrics.parquet_objects.inc()
            await self._committer.commit(partition, record.offset + 1)
            if self._metrics is not None:
                self._metrics.records.labels(outcome="accepted").inc()
                self._set_committed_metric(partition, record.offset + 1)
                self._metrics.mark_progress()
            self._next_offsets[partition] = record.offset + 1

    def _set_committed_metric(self, partition: Partition, next_offset: int) -> None:
        assert self._metrics is not None
        self._metrics.committed_offset.labels(
            topic=partition.topic,
            partition=str(partition.partition),
        ).set(next_offset)

    async def flush_all(self, partitions: set[Partition] | None = None) -> None:
        # One broker record is already a complete durability unit. Acquiring the
        # lock is still useful at shutdown: it waits for any in-flight unit.
        _ = partitions
        async with self._generation_lock:
            return None
