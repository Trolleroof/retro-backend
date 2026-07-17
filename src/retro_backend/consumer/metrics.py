"""Consumer metrics kept separate from API metric registration."""

from __future__ import annotations

from prometheus_client import CollectorRegistry, Counter, Gauge


class ConsumerMetrics:
    def __init__(self, registry: CollectorRegistry | None = None) -> None:
        self.records = Counter(
            "retro_consumer_records_total",
            "Source records handled by outcome",
            ("outcome",),
            registry=registry,
        )
        self.parquet_objects = Counter(
            "retro_consumer_parquet_objects_total",
            "Parquet objects persisted",
            registry=registry,
        )
        self.committed_offset = Gauge(
            "retro_consumer_committed_next_offset",
            "Latest committed Kafka next offset",
            ("topic", "partition"),
            registry=registry,
        )
