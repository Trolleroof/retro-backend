import asyncio
import json
from pathlib import Path

import pytest

from retro_backend.consumer.service import ConsumerService
from retro_backend.consumer.types import BrokerRecord, Partition

FIXTURE = (
    Path(__file__).resolve().parents[2]
    / "contracts"
    / "fixtures"
    / "interaction-trace-chunk-v2.json"
)


def record(offset: int) -> BrokerRecord:
    payload = json.loads(FIXTURE.read_text())
    return BrokerRecord(
        topic="retro-game-events",
        partition=0,
        offset=offset,
        timestamp_ms=1_700_000_000_000,
        key=None,
        value=json.dumps(payload).encode(),
        headers=(("retro-user-id", payload["user_id"].encode()),),
    )


class FakeSource:
    def __init__(self, stop: asyncio.Event) -> None:
        self.stop = stop
        self.started = False
        self.stopped = False
        self.listener: object | None = None
        self.polls = 0

    async def start(self, listener: object) -> None:
        self.started = True
        self.listener = listener

    async def stop_source(self) -> None:
        self.stopped = True

    async def poll(self, *, timeout_ms: int, max_records: int) -> list[BrokerRecord]:
        _ = (timeout_ms, max_records)
        self.polls += 1
        if self.polls == 1:
            return [record(7)]
        self.stop.set()
        return []


class FakeWorker:
    def __init__(self) -> None:
        self.handled: list[BrokerRecord] = []
        self.flushes: list[set[Partition] | None] = []

    async def handle(self, record: BrokerRecord) -> None:
        self.handled.append(record)

    async def flush_all(self, partitions: set[Partition] | None = None) -> None:
        self.flushes.append(partitions)


@pytest.mark.asyncio
async def test_service_runs_source_and_flushes_on_shutdown() -> None:
    stop, worker = asyncio.Event(), FakeWorker()
    source = FakeSource(stop)
    service = ConsumerService(
        source=source,
        worker=worker,
        stop_event=stop,
        poll_timeout_ms=10,
        poll_max_records=10,
        flush_interval_seconds=60,
    )

    await service.run()

    assert source.started and source.stopped
    assert [value.offset for value in worker.handled] == [7]
    assert worker.flushes[-1] is None


@pytest.mark.asyncio
async def test_rebalance_listener_flushes_only_revoked_partitions() -> None:
    stop, worker = asyncio.Event(), FakeWorker()
    source = FakeSource(stop)
    service = ConsumerService(
        source=source,
        worker=worker,
        stop_event=stop,
        poll_timeout_ms=10,
        poll_max_records=10,
        flush_interval_seconds=60,
    )
    revoked = {Partition("events", 2), Partition("events", 3)}

    await service.on_partitions_revoked(revoked)

    assert worker.flushes == [revoked]
