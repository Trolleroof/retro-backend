from typing import Any, cast

import pytest

from retro_backend.consumer.kafka import AiokafkaRecordSource
from retro_backend.consumer.types import Partition


class RawRecord:
    topic = "events"
    partition = 2
    offset = 7
    timestamp = 1234
    key = b"key"
    value = b"value"
    headers = [("retro-user-id", b"usr_1")]


class FakeConsumer:
    def __init__(self) -> None:
        self.subscription: tuple[list[str], object] | None = None
        self.started = False
        self.stopped = False
        self.commits: list[dict[object, object]] = []

    def subscribe(self, topics: list[str], listener: object) -> None:
        self.subscription = (topics, listener)

    async def start(self) -> None:
        self.started = True

    async def stop(self) -> None:
        self.stopped = True

    async def getmany(self, **_: object) -> dict[object, list[RawRecord]]:
        return {object(): [RawRecord()]}

    async def commit(self, offsets: dict[object, object]) -> None:
        self.commits.append(offsets)


class Listener:
    async def on_partitions_revoked(self, partitions: set[Partition]) -> None:
        _ = partitions
        return None

    async def on_partitions_assigned(self, partitions: set[Partition]) -> None:
        _ = partitions
        return None


@pytest.mark.asyncio
async def test_aiokafka_source_disables_transport_details_at_domain_boundary() -> None:
    consumer = FakeConsumer()
    source = AiokafkaRecordSource(consumer, topic="events")

    await source.start(Listener())
    records = await source.poll(timeout_ms=50, max_records=5)
    await source.commit(Partition("events", 2), 8)
    await source.stop_source()

    assert consumer.started and consumer.stopped
    assert consumer.subscription and consumer.subscription[0] == ["events"]
    assert records[0].offset == 7
    assert records[0].headers == (("retro-user-id", b"usr_1"),)
    committed = cast(Any, next(iter(consumer.commits[0].values())))
    assert committed.offset == 8
