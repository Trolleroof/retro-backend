"""Transport-neutral consumer domain types."""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class Partition:
    topic: str
    partition: int


@dataclass(frozen=True, slots=True)
class BrokerRecord:
    topic: str
    partition: int
    offset: int
    timestamp_ms: int
    key: bytes | None
    value: bytes
    headers: tuple[tuple[str, bytes], ...]

    @property
    def topic_partition(self) -> Partition:
        return Partition(self.topic, self.partition)


@dataclass(frozen=True, slots=True)
class EventRow:
    schema_version: int
    user_id: str
    game_id: str
    game_type: str
    game_version: str
    chunk_id: str
    chunk_sequence: int
    event_id: str
    event_sequence: int
    event_type: str
    event_version: int
    visibility: str
    client_time_ms: float
    monotonic_time_us: int
    simulation_tick: int | None
    payload_json: str
    broker_partition: int
    broker_offset: int
    ingested_at_ms: int


@dataclass(frozen=True, slots=True)
class EventBatch:
    partition: Partition
    start_offset: int
    end_offset: int
    record_count: int
    rows: tuple[EventRow, ...]
