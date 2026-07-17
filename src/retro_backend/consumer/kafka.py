"""aiokafka source and manual offset-commit adapter."""

from __future__ import annotations

from collections.abc import Iterable
from typing import Any

from aiokafka import TopicPartition
from aiokafka.abc import ConsumerRebalanceListener
from aiokafka.structs import OffsetAndMetadata
from typing_extensions import override

from .service import PartitionListener
from .types import BrokerRecord, Partition


class _RebalanceAdapter(ConsumerRebalanceListener):
    def __init__(self, listener: PartitionListener) -> None:
        self._listener = listener

    @staticmethod
    def _partitions(values: Iterable[TopicPartition]) -> set[Partition]:
        return {Partition(value.topic, value.partition) for value in values}

    @override
    async def on_partitions_revoked(  # pyright: ignore[reportIncompatibleMethodOverride]
        self, revoked: set[TopicPartition]
    ) -> None:
        await self._listener.on_partitions_revoked(self._partitions(revoked))

    @override
    async def on_partitions_assigned(  # pyright: ignore[reportIncompatibleMethodOverride]
        self, assigned: set[TopicPartition]
    ) -> None:
        await self._listener.on_partitions_assigned(self._partitions(assigned))


class AiokafkaRecordSource:
    def __init__(self, consumer: Any, *, topic: str) -> None:
        self._consumer = consumer
        self._topic = topic

    async def start(self, listener: PartitionListener) -> None:
        self._consumer.subscribe([self._topic], listener=_RebalanceAdapter(listener))
        await self._consumer.start()

    async def stop_source(self) -> None:
        await self._consumer.stop()

    async def poll(self, *, timeout_ms: int, max_records: int) -> list[BrokerRecord]:
        batches = await self._consumer.getmany(
            timeout_ms=timeout_ms,
            max_records=max_records,
        )
        records = [self._to_domain(record) for values in batches.values() for record in values]
        return sorted(records, key=lambda value: (value.topic, value.partition, value.offset))

    async def commit(self, partition: Partition, next_offset: int) -> None:
        await self._consumer.commit(
            {
                TopicPartition(partition.topic, partition.partition): OffsetAndMetadata(
                    next_offset, ""
                )
            }
        )

    @staticmethod
    def _to_domain(record: Any) -> BrokerRecord:
        value = record.value
        if not isinstance(value, bytes):
            raise TypeError("Kafka record value must be bytes")
        key = record.key
        if key is not None and not isinstance(key, bytes):
            raise TypeError("Kafka record key must be bytes or null")
        return BrokerRecord(
            topic=str(record.topic),
            partition=int(record.partition),
            offset=int(record.offset),
            timestamp_ms=int(record.timestamp),
            key=key,
            value=value,
            headers=tuple(record.headers or ()),
        )
