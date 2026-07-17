"""Consumer progress and liveness metrics."""

from __future__ import annotations

import time

from prometheus_client import REGISTRY, CollectorRegistry, Counter, Gauge


class ConsumerMetrics:
    def __init__(self, registry: CollectorRegistry | None = None) -> None:
        self.registry = registry if registry is not None else REGISTRY
        self.records = Counter(
            "retro_consumer_records_total",
            "Source records handled by outcome",
            ("outcome",),
            registry=self.registry,
        )
        self.parquet_objects = Counter(
            "retro_consumer_parquet_objects_total",
            "Parquet objects persisted",
            registry=self.registry,
        )
        self.committed_offset = Gauge(
            "retro_consumer_committed_next_offset",
            "Latest committed Kafka next offset",
            ("topic", "partition"),
            registry=self.registry,
        )
        self.running = Gauge(
            "retro_consumer_running",
            "Whether the consumer poll loop is running",
            registry=self.registry,
        )
        self.last_heartbeat = Gauge(
            "retro_consumer_last_heartbeat_timestamp_seconds",
            "Unix time of the latest completed consumer poll",
            registry=self.registry,
        )
        self.last_progress = Gauge(
            "retro_consumer_last_progress_timestamp_seconds",
            "Unix time of the latest durable record commit",
            registry=self.registry,
        )

    def mark_running(self, *, now: float | None = None) -> None:
        timestamp = time.time() if now is None else now
        self.running.set(1)
        self.last_heartbeat.set(timestamp)

    def mark_heartbeat(self, *, now: float | None = None) -> None:
        self.last_heartbeat.set(time.time() if now is None else now)

    def mark_progress(self, *, now: float | None = None) -> None:
        self.last_progress.set(time.time() if now is None else now)

    def mark_stopped(self) -> None:
        self.running.set(0)
