from prometheus_client import REGISTRY, CollectorRegistry, generate_latest

from retro_backend.consumer.healthcheck import metrics_are_healthy
from retro_backend.consumer.metrics import ConsumerMetrics


def test_default_consumer_metrics_are_exported() -> None:
    metrics = ConsumerMetrics()
    metrics.records.labels(outcome="accepted").inc()

    exported = generate_latest(REGISTRY)

    assert b'retro_consumer_records_total{outcome="accepted"} 1.0' in exported


def test_custom_registry_contains_consumer_metrics_and_progress_indicators() -> None:
    registry = CollectorRegistry()
    metrics = ConsumerMetrics(registry)

    metrics.mark_running(now=100.0)
    metrics.mark_heartbeat(now=101.0)
    metrics.mark_progress(now=102.0)
    exported = generate_latest(registry)

    assert b"retro_consumer_running 1.0" in exported
    assert b"retro_consumer_last_heartbeat_timestamp_seconds 101.0" in exported
    assert b"retro_consumer_last_progress_timestamp_seconds 102.0" in exported


def test_consumer_health_requires_running_and_fresh_heartbeat() -> None:
    registry = CollectorRegistry()
    metrics = ConsumerMetrics(registry)
    metrics.mark_running(now=100.0)
    metrics.mark_heartbeat(now=100.0)

    assert metrics_are_healthy(generate_latest(registry), now=119.0, stale_seconds=20.0)
    assert not metrics_are_healthy(generate_latest(registry), now=121.0, stale_seconds=20.0)

    metrics.mark_stopped()
    assert not metrics_are_healthy(generate_latest(registry), now=101.0, stale_seconds=20.0)


def test_consumer_health_rejects_missing_or_malformed_metrics() -> None:
    assert not metrics_are_healthy(b"", now=1.0, stale_seconds=20.0)
    assert not metrics_are_healthy(b"retro_consumer_running nope\n", now=1.0, stale_seconds=20.0)
