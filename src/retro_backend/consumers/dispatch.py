"""Sequential per-message dispatch for Cloudflare Queue batches."""

import hashlib
import json
import math
from contextlib import suppress
from datetime import UTC, datetime
from typing import cast

from ..bindings import QueueBatch, QueueMessage, iter_binding_values, value_field
from .episodes import EpisodeConsumer as EpisodeConsumer
from .gameplay import GameplayConsumer as GameplayConsumer
from .gameplay import GameplayDeadLetterConsumer as GameplayDeadLetterConsumer
from .gameplay import describe_gameplay_error

GAMEPLAY_QUEUE_NAME = "retro-gameplay-events"
GAMEPLAY_DLQ_NAME = "retro-gameplay-events-dlq"
EPISODE_QUEUE_NAME = "retro-episode-events"

# Mirrors ``max_retries`` on the ``retro-gameplay-events`` consumer in
# wrangler.toml. A delivery at or beyond this attempt is the last one before
# Cloudflare dead-letters the message, so that is when the failure note that
# makes the DLQ archive self-diagnosing is worth an extra small R2 write.
GAMEPLAY_MAX_DELIVERY_ATTEMPTS = 5


def _delivery_attempts(queue_message: QueueMessage) -> int | None:
    """Read the 1-based Queue delivery attempt, or ``None`` when unavailable."""
    attempts = value_field(queue_message, "attempts")
    if isinstance(attempts, bool) or not isinstance(attempts, int) or attempts < 1:
        return None
    return attempts


def _is_final_delivery(attempts: int | None) -> bool:
    # Without the attempt counter we cannot tell, so record the note every time
    # rather than lose the only copy of the underlying error.
    return attempts is None or attempts >= GAMEPLAY_MAX_DELIVERY_ATTEMPTS


def _safe_retry(queue_message: QueueMessage) -> None:
    """Ask for redelivery, never letting one message's failure strand another.

    Settling is the only thing that stands between a failed message and silent
    data loss, so it must not depend on any other work succeeding.
    """
    try:
        queue_message.retry()
    except Exception:
        return


def _retry_all(queue_messages: list[QueueMessage]) -> None:
    for queue_message in queue_messages:
        _safe_retry(queue_message)


def _log_retry(
    queue_name: object,
    message_id: object,
    error: BaseException,
    attempts: int | None,
) -> None:
    """Log retry metadata without logging message bodies or user data."""
    message_digest = (
        hashlib.sha256(message_id.encode("utf-8")).hexdigest()
        if isinstance(message_id, str)
        else None
    )
    described = describe_gameplay_error(error)
    print(json.dumps({
        "event": "queue_message_retry",
        "queue": queue_name,
        "message_id_sha256": message_digest,
        "attempts": attempts,
        "final_attempt": _is_final_delivery(attempts),
        "error_type": described["error_type"],
        "error_message": described["error_message"],
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
    consumer_type: type[GameplayConsumer | GameplayDeadLetterConsumer | EpisodeConsumer]
    if queue_name == GAMEPLAY_QUEUE_NAME:
        consumer_type = GameplayConsumer
    elif queue_name == GAMEPLAY_DLQ_NAME:
        consumer_type = GameplayDeadLetterConsumer
    elif queue_name == EPISODE_QUEUE_NAME:
        consumer_type = EpisodeConsumer
    else:
        raise RuntimeError(f"unsupported Queue: {queue_name}")

    queue_messages = [
        cast(QueueMessage, message)
        for message in iter_binding_values(value_field(queue_batch, "messages"))
    ]
    try:
        consumer = consumer_type(environment)
    except Exception:
        _retry_all(queue_messages)
        return

    # Anything still in here when the handler unwinds has been neither acked nor
    # retried. An uncaught exception in a Queue handler makes Cloudflare redeliver
    # the *whole* batch, so an unsettled tail replays the same failing work on
    # every attempt and is dead-lettered together once the retries run out.
    unsettled = list(queue_messages)
    try:
        for queue_message in queue_messages:
            body: object = None
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
                # Settle before doing anything else. Once the Workers Python
                # runtime has spent its CPU budget every further operation
                # re-raises ``CpuLimitExceeded``, so diagnostics that run first
                # take the retry() call down with them and lose the message.
                queue_message.retry()
                unsettled.remove(queue_message)
                await _report_failure(
                    consumer,
                    queue_message,
                    body,
                    error,
                    queue_name,
                )
            else:
                queue_message.ack()
                unsettled.remove(queue_message)
    finally:
        _retry_all(unsettled)


async def _report_failure(
    consumer: object,
    queue_message: QueueMessage,
    pointer_body: object,
    error: BaseException,
    queue_name: object,
) -> None:
    """Persist and log why a message failed, without ever raising.

    Every step here is a diagnostic. The message has already been settled by the
    time this runs, and nothing in it may unsettle the rest of the batch by
    escaping into the dispatch loop.
    """
    attempts = _delivery_attempts(queue_message)
    record = getattr(consumer, "record_processing_failure", None)
    if record is not None and _is_final_delivery(attempts):
        with suppress(Exception):
            await record(pointer_body, error, attempts=attempts)
    try:
        _log_retry(queue_name, value_field(queue_message, "id"), error, attempts)
    except Exception:
        return
