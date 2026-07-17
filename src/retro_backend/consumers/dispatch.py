"""Sequential per-message dispatch for Cloudflare Queue batches."""

import math
from datetime import UTC, datetime
from typing import cast

from ..bindings import QueueBatch, QueueMessage, iter_binding_values, value_field
from ..contracts import TransientGameplayError
from .gameplay import GameplayConsumer

GAMEPLAY_QUEUE_NAME = "retro-gameplay-events"


def _normalize_message_timestamp_ms(value: object | None) -> int | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value if value >= 0 else None
    if isinstance(value, float):
        return int(value) if math.isfinite(value) and value >= 0 else None
    if isinstance(value, datetime):
        normalized = value if value.tzinfo is not None else value.replace(tzinfo=UTC)
        timestamp_ms = int(normalized.timestamp() * 1000)
        return timestamp_ms if timestamp_ms >= 0 else None
    get_time = getattr(value, "getTime", None)
    if callable(get_time):
        timestamp_ms = get_time()
        if (
            not isinstance(timestamp_ms, bool)
            and isinstance(timestamp_ms, (int, float))
            and math.isfinite(timestamp_ms)
            and timestamp_ms >= 0
        ):
            return int(timestamp_ms)
    return None


async def dispatch_queue(batch: object, environment: object) -> None:
    queue_batch = cast(QueueBatch, batch)
    queue_name = value_field(queue_batch, "queue")
    if queue_name != GAMEPLAY_QUEUE_NAME:
        raise RuntimeError(f"unsupported Queue: {queue_name}")

    consumer = GameplayConsumer(environment)
    messages = value_field(queue_batch, "messages")
    for message in iter_binding_values(messages):
        queue_message = cast(QueueMessage, message)
        body = value_field(queue_message, "body")
        message_id = value_field(queue_message, "id")
        message_timestamp_ms = _normalize_message_timestamp_ms(
            value_field(queue_message, "timestamp")
        )
        try:
            await consumer.process(
                body,
                message_id=message_id,
                message_timestamp_ms=message_timestamp_ms,
            )
        except TransientGameplayError:
            queue_message.retry()
        else:
            queue_message.ack()
