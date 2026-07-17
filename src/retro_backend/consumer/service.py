"""Polling lifecycle, timed flush, and rebalance coordination."""

from __future__ import annotations

import asyncio
import logging
from typing import Protocol

from .metrics import ConsumerMetrics
from .types import BrokerRecord, Partition

logger = logging.getLogger(__name__)


class PartitionListener(Protocol):
    async def on_partitions_revoked(self, partitions: set[Partition]) -> None: ...

    async def on_partitions_assigned(self, partitions: set[Partition]) -> None: ...


class RecordSource(Protocol):
    async def start(self, listener: PartitionListener) -> None: ...

    async def stop_source(self) -> None: ...

    async def poll(self, *, timeout_ms: int, max_records: int) -> list[BrokerRecord]: ...


class RecordWorker(Protocol):
    async def handle(self, record: BrokerRecord) -> None: ...

    async def flush_all(self, partitions: set[Partition] | None = None) -> None: ...

    async def assign(self, partitions: set[Partition]) -> None: ...

    async def revoke(self, partitions: set[Partition]) -> None: ...


class ConsumerService:
    def __init__(
        self,
        *,
        source: RecordSource,
        worker: RecordWorker,
        stop_event: asyncio.Event,
        poll_timeout_ms: int,
        poll_max_records: int,
        flush_interval_seconds: float,
        metrics: ConsumerMetrics | None = None,
    ) -> None:
        self._source = source
        self._worker = worker
        self._stop_event = stop_event
        self._poll_timeout_ms = poll_timeout_ms
        self._poll_max_records = poll_max_records
        self._flush_interval_seconds = flush_interval_seconds
        self._metrics = metrics

    async def run(self) -> None:
        await self._source.start(self)
        if self._metrics is not None:
            self._metrics.mark_running()
        next_flush = asyncio.get_running_loop().time() + self._flush_interval_seconds
        try:
            while not self._stop_event.is_set():
                records = await self._source.poll(
                    timeout_ms=self._poll_timeout_ms,
                    max_records=self._poll_max_records,
                )
                if self._metrics is not None:
                    self._metrics.mark_heartbeat()
                for record in records:
                    await self._worker.handle(record)
                    if self._metrics is not None:
                        self._metrics.mark_heartbeat()
                now = asyncio.get_running_loop().time()
                if now >= next_flush:
                    await self._worker.flush_all()
                    next_flush = now + self._flush_interval_seconds
        finally:
            if self._metrics is not None:
                self._metrics.mark_stopped()
            try:
                await self._worker.flush_all()
            finally:
                await self._source.stop_source()

    async def on_partitions_revoked(self, partitions: set[Partition]) -> None:
        logger.info("revoking partitions", extra={"partition_count": len(partitions)})
        await self._worker.revoke(partitions)

    async def on_partitions_assigned(self, partitions: set[Partition]) -> None:
        logger.info("partitions assigned", extra={"partition_count": len(partitions)})
        await self._worker.assign(partitions)
