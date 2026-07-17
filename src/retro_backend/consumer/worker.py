"""Transport-neutral batch and acknowledgment ordering."""

from __future__ import annotations

from typing import Protocol

from .buffer import OffsetGapError, PartitionBuffer
from .metrics import ConsumerMetrics
from .s3_writer import BatchWriter
from .types import BrokerRecord, Partition
from .validation import RecordValidationError, decode_v2_record


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
        max_records: int,
        max_rows: int,
        max_bytes: int,
        metrics: ConsumerMetrics | None = None,
    ) -> None:
        self._committer = committer
        self._batch_writer = batch_writer
        self._dlq = dlq
        self._limits = (max_records, max_rows, max_bytes)
        self._buffers: dict[Partition, PartitionBuffer] = {}
        self._next_offsets: dict[Partition, int] = {}
        self._metrics = metrics

    def _buffer(self, partition: Partition) -> PartitionBuffer:
        if partition not in self._buffers:
            self._buffers[partition] = PartitionBuffer(
                max_records=self._limits[0],
                max_rows=self._limits[1],
                max_bytes=self._limits[2],
            )
        return self._buffers[partition]

    def _assert_contiguous(self, record: BrokerRecord) -> None:
        expected = self._next_offsets.get(record.topic_partition)
        if expected is not None and record.offset != expected:
            raise OffsetGapError(f"expected offset {expected}, received {record.offset}")

    async def handle(self, record: BrokerRecord) -> None:
        self._assert_contiguous(record)
        partition = record.topic_partition
        try:
            rows = decode_v2_record(record)
        except RecordValidationError as error:
            await self.flush(partition)
            await self._dlq.publish(record, str(error))
            await self._committer.commit(partition, record.offset + 1)
            if self._metrics is not None:
                self._metrics.records.labels(outcome="dlq").inc()
                self._metrics.committed_offset.labels(
                    topic=partition.topic, partition=str(partition.partition)
                ).set(record.offset + 1)
            self._next_offsets[partition] = record.offset + 1
            return

        buffer = self._buffer(partition)
        record_day = record.timestamp_ms // 86_400_000
        if buffer.utc_day is not None and buffer.utc_day != record_day:
            await self.flush(partition)
            buffer = self._buffer(partition)
        should_flush = buffer.add(record, rows)
        self._next_offsets[partition] = record.offset + 1
        if self._metrics is not None:
            self._metrics.records.labels(outcome="accepted").inc()
        if should_flush:
            await self.flush(partition)

    async def flush(self, partition: Partition) -> None:
        buffer = self._buffers.get(partition)
        if buffer is None or buffer.empty:
            return
        batch = buffer.batch()
        await self._batch_writer.write(batch)
        if self._metrics is not None:
            self._metrics.parquet_objects.inc()
        await self._committer.commit(partition, batch.end_offset + 1)
        if self._metrics is not None:
            self._metrics.committed_offset.labels(
                topic=partition.topic, partition=str(partition.partition)
            ).set(batch.end_offset + 1)
        del self._buffers[partition]

    async def flush_all(self, partitions: set[Partition] | None = None) -> None:
        selected = partitions if partitions is not None else set(self._buffers)
        for partition in sorted(selected, key=lambda value: (value.topic, value.partition)):
            await self.flush(partition)
