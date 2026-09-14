from types import SimpleNamespace

import pytest

from retro_backend.queue_health import check_queue_health


class MetricsQueue:
    def __init__(self, metrics: object) -> None:
        self._metrics = metrics

    async def metrics(self) -> object:
        return self._metrics


def healthy_environment() -> SimpleNamespace:
    metrics = {"backlog_count": 0, "oldest_message_timestamp_ms": 0}
    return SimpleNamespace(
        GAMEPLAY_QUEUE=MetricsQueue(metrics),
        GAMEPLAY_DLQ=MetricsQueue(metrics),
        GAMEPLAY_ARCHIVE_DLQ=MetricsQueue(metrics),
        EPISODE_QUEUE=MetricsQueue(metrics),
        EPISODE_DLQ=MetricsQueue(metrics),
    )


@pytest.mark.asyncio
async def test_queue_health_accepts_empty_queues() -> None:
    await check_queue_health(healthy_environment(), now_ms=1_800_000_000_000)


@pytest.mark.asyncio
async def test_queue_health_fails_on_any_archive_message() -> None:
    environment = healthy_environment()
    environment.GAMEPLAY_ARCHIVE_DLQ = MetricsQueue(
        {"backlogCount": 1, "oldestMessageTimestamp": 1_800_000_000_000}
    )

    with pytest.raises(RuntimeError, match="archive-dlq: backlog_count=1"):
        await check_queue_health(environment, now_ms=1_800_000_000_001)


@pytest.mark.asyncio
async def test_queue_health_fails_on_stale_main_message() -> None:
    environment = healthy_environment()
    environment.GAMEPLAY_QUEUE = MetricsQueue(
        {"backlog_count": 1, "oldest_message_timestamp_ms": 1_799_999_000_000}
    )

    with pytest.raises(RuntimeError, match="gameplay-events: oldest_message_age_minutes=16"):
        await check_queue_health(environment, now_ms=1_800_000_000_000)


@pytest.mark.asyncio
async def test_queue_health_fails_on_any_stranded_episode_message() -> None:
    """The episode DLQ has no consumer, so one message there is already loss."""
    environment = healthy_environment()
    environment.EPISODE_DLQ = MetricsQueue(
        {"backlogCount": 1, "oldestMessageTimestamp": 1_800_000_000_000}
    )

    with pytest.raises(RuntimeError, match="retro-episode-events-dlq: backlog_count=1"):
        await check_queue_health(environment, now_ms=1_800_000_000_001)


@pytest.mark.asyncio
async def test_queue_health_fails_on_a_stale_episode_message() -> None:
    environment = healthy_environment()
    environment.EPISODE_QUEUE = MetricsQueue(
        {"backlog_count": 1, "oldest_message_timestamp_ms": 1_799_999_000_000}
    )

    with pytest.raises(RuntimeError, match="retro-episode-events: oldest_message_age_minutes=16"):
        await check_queue_health(environment, now_ms=1_800_000_000_000)


@pytest.mark.asyncio
async def test_queue_health_fails_when_an_episode_binding_is_missing() -> None:
    environment = healthy_environment()
    del environment.EPISODE_QUEUE

    with pytest.raises(RuntimeError, match="retro-episode-events: binding unavailable"):
        await check_queue_health(environment, now_ms=1_800_000_000_000)
