"""Bounded, contiguous buffering for one topic-partition."""

from __future__ import annotations

from .types import BrokerRecord, EventBatch, EventRow, Partition


class OffsetGapError(RuntimeError):
    pass


class PartitionBuffer:
    def __init__(self, *, max_records: int, max_rows: int, max_bytes: int) -> None:
        self._max_records = max_records
        self._max_rows = max_rows
        self._max_bytes = max_bytes
        self._partition: Partition | None = None
        self._start_offset: int | None = None
        self._end_offset: int | None = None
        self._record_count = 0
        self._source_bytes = 0
        self._rows: list[EventRow] = []
        self._utc_day: int | None = None

    @property
    def empty(self) -> bool:
        return self._record_count == 0

    @property
    def utc_day(self) -> int | None:
        return self._utc_day

    def add(self, record: BrokerRecord, rows: list[EventRow]) -> bool:
        partition = record.topic_partition
        if self._partition is not None and partition != self._partition:
            raise ValueError("a partition buffer cannot mix topic-partitions")
        if self._end_offset is not None and record.offset != self._end_offset + 1:
            raise OffsetGapError(
                f"expected offset {self._end_offset + 1}, received {record.offset}"
            )
        self._partition = partition
        self._utc_day = (
            record.timestamp_ms // 86_400_000 if self._utc_day is None else self._utc_day
        )
        self._start_offset = record.offset if self._start_offset is None else self._start_offset
        self._end_offset = record.offset
        self._record_count += 1
        self._source_bytes += len(record.value)
        self._rows.extend(rows)
        return (
            self._record_count >= self._max_records
            or len(self._rows) >= self._max_rows
            or self._source_bytes >= self._max_bytes
        )

    def batch(self) -> EventBatch:
        if self._partition is None or self._start_offset is None or self._end_offset is None:
            raise ValueError("cannot build an empty batch")
        return EventBatch(
            partition=self._partition,
            start_offset=self._start_offset,
            end_offset=self._end_offset,
            record_count=self._record_count,
            rows=tuple(self._rows),
        )
