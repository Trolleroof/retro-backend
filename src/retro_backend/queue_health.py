"""Small scheduled health check for the gameplay queues."""

import json
import time
from collections.abc import Mapping
from typing import cast

from .bindings import QueueBinding, is_binding_null, value_field

QUEUE_CHECKS = (
    ("GAMEPLAY_QUEUE", "retro-gameplay-events", False),
    ("GAMEPLAY_DLQ", "retro-gameplay-events-dlq", False),
    ("GAMEPLAY_ARCHIVE_DLQ", "retro-gameplay-events-archive-dlq", True),
)
MAX_AGE_MS = 10 * 60 * 1000


def _metric(metrics: object, snake: str, camel: str) -> object | None:
    if isinstance(metrics, Mapping):
        return metrics.get(snake, metrics.get(camel))
    return getattr(metrics, camel, getattr(metrics, snake, None))


def _timestamp_ms(value: object | None) -> int | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        return int(value) if value >= 0 else None
    get_time = getattr(value, "getTime", None)
    if callable(get_time):
        result = get_time()
        return int(result) if isinstance(result, (int, float)) and result >= 0 else None
    return None


async def check_queue_health(environment: object, *, now_ms: int | None = None) -> None:
    now = int(time.time() * 1000) if now_ms is None else now_ms
    violations: list[str] = []
    for binding_name, queue_name, terminal in QUEUE_CHECKS:
        queue = value_field(environment, binding_name)
        if is_binding_null(queue):
            violations.append(f"{queue_name}: binding unavailable")
            continue
        metrics = await cast(QueueBinding, queue).metrics()
        count_value = _metric(metrics, "backlog_count", "backlogCount")
        count = int(count_value) if isinstance(count_value, (int, float)) else 0
        oldest_ms = _timestamp_ms(
            _metric(metrics, "oldest_message_timestamp_ms", "oldestMessageTimestamp")
        )
        if terminal and count > 0:
            violations.append(f"{queue_name}: backlog_count={count}")
        elif count > 0 and oldest_ms is not None and now - oldest_ms > MAX_AGE_MS:
            age_minutes = (now - oldest_ms) // 60_000
            violations.append(f"{queue_name}: oldest_message_age_minutes={age_minutes}")

    if violations:
        print(json.dumps({"event": "queue_health_failed", "violations": violations}))
        raise RuntimeError("; ".join(violations))
