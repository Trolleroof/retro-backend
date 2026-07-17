"""Redpanda producer interface and aiokafka implementation."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol

from aiokafka import AIOKafkaProducer

from .config import Settings


@dataclass(frozen=True)
class BrokerMetadata:
    partition: int
    offset: int


class EventProducer(Protocol):
    async def start(self) -> None: ...

    async def stop(self) -> None: ...

    async def send(
        self,
        *,
        topic: str,
        value: bytes,
        key: bytes,
        headers: list[tuple[str, bytes]],
    ) -> BrokerMetadata: ...


class KafkaEventProducer:
    def __init__(self, settings: Settings) -> None:
        self._settings = settings
        self._producer: AIOKafkaProducer | None = None

    async def start(self) -> None:
        producer = AIOKafkaProducer(
            bootstrap_servers=self._settings.kafka_bootstrap_servers,
            acks="all",
            enable_idempotence=True,
            compression_type="zstd",
        )
        await producer.start()
        self._producer = producer

    async def stop(self) -> None:
        if self._producer is not None:
            await self._producer.stop()
            self._producer = None

    async def send(
        self,
        *,
        topic: str,
        value: bytes,
        key: bytes,
        headers: list[tuple[str, bytes]],
    ) -> BrokerMetadata:
        if self._producer is None:
            raise RuntimeError("event producer is not started")
        metadata = await self._producer.send_and_wait(
            topic,
            value=value,
            key=key,
            headers=headers,
        )
        return BrokerMetadata(partition=metadata.partition, offset=metadata.offset)
