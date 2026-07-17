"""Acknowledged publication of rejected source records."""

from __future__ import annotations

from typing import Any

from .types import BrokerRecord


class KafkaDlqPublisher:
    def __init__(self, producer: Any, *, topic: str) -> None:
        self._producer = producer
        self._topic = topic

    async def publish(self, value: BrokerRecord, reason: str) -> None:
        headers = list(value.headers)
        headers.extend(
            [
                ("retro-dlq-reason", reason[:1000].encode("utf-8", errors="replace")),
                ("retro-source-topic", value.topic.encode()),
                ("retro-source-partition", str(value.partition).encode()),
                ("retro-source-offset", str(value.offset).encode()),
            ]
        )
        await self._producer.send_and_wait(
            self._topic,
            key=value.key,
            value=value.value,
            headers=headers,
            timestamp_ms=value.timestamp_ms,
        )
