"""Sequential per-message dispatch for Cloudflare Queue batches."""

import hashlib
import json
import math
from datetime import UTC, datetime
from typing import cast

from ..bindings import QueueBatch, QueueMessage, iter_binding_values, value_field
from .gameplay import GameplayConsumer as GameplayConsumer
from .gameplay import GameplayDeadLetterConsumer as GameplayDeadLetterConsumer

GAMEPLAY_QUEUE_NAME = "retro-gameplay-events"
GAMEPLAY_DLQ_NAME = "retro-gameplay-events-dlq"


def _log_retry(queue_name: object, message_id: object, error: Exception) -> None:
    """Log retry metadata without logging message bodies or user data."""
    message_digest = (
        hashlib.sha256(message_id.encode("utf-8")).hexdigest()
        if isinstance(message_id, str)
        else None
    )
    print(json.dumps({
        "event": "queue_message_retry",
        "queue": queue_name,
        "message_id_sha256": message_digest,
        "error_type": type(error).__name__,
    }, separators=(",", ":")))


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
    if queue_name == GAMEPLAY_QUEUE_NAME:
        consumer_type = GameplayConsumer
    elif queue_name == GAMEPLAY_DLQ_NAME:
        consumer_type = GameplayDeadLetterConsumer
    else:
        raise RuntimeError(f"unsupported Queue: {queue_name}")

    messages = value_field(queue_batch, "messages")
    try:
        consumer = consumer_type(environment)
    except Exception:
        for message in iter_binding_values(messages):
            cast(QueueMessage, message).retry()
        return

    for message in iter_binding_values(messages):
        queue_message = cast(QueueMessage, message)
        try:
            body = value_field(queue_message, "body")
            message_id = value_field(queue_message, "id")
            message_timestamp_ms = _normalize_message_timestamp_ms(
                value_field(queue_message, "timestamp")
            )
            await consumer.process(
                body,
                message_id=message_id,
                message_timestamp_ms=message_timestamp_ms,
            )
        except Exception as error:
            _log_retry(queue_name, value_field(queue_message, "id"), error)
            queue_message.retry()
        else:
            queue_message.ack()
