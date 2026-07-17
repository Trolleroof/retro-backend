"""Fixed gameplay-event Parquet schema and deterministic encoding."""

from __future__ import annotations

import io
from collections.abc import Iterable
from dataclasses import asdict
from typing import Final

import pyarrow as pa
import pyarrow.parquet as pq

from .types import EventRow

PARQUET_FIELDS: Final[tuple[str, ...]] = (
    "schema_version",
    "user_id",
    "game_id",
    "game_type",
    "game_version",
    "chunk_id",
    "chunk_sequence",
    "event_id",
    "event_sequence",
    "event_type",
    "event_version",
    "visibility",
    "client_time_ms",
    "monotonic_time_us",
    "simulation_tick",
    "payload_json",
    "broker_partition",
    "broker_offset",
    "ingested_at_ms",
)

PARQUET_SCHEMA: Final[pa.Schema] = pa.schema(
    [
        pa.field("schema_version", pa.int16(), nullable=False),
        pa.field("user_id", pa.string(), nullable=False),
        pa.field("game_id", pa.string(), nullable=False),
        pa.field("game_type", pa.string(), nullable=False),
        pa.field("game_version", pa.string(), nullable=False),
        pa.field("chunk_id", pa.string(), nullable=False),
        pa.field("chunk_sequence", pa.int64(), nullable=False),
        pa.field("event_id", pa.string(), nullable=False),
        pa.field("event_sequence", pa.int64(), nullable=False),
        pa.field("event_type", pa.string(), nullable=False),
        pa.field("event_version", pa.int16(), nullable=False),
        pa.field("visibility", pa.string(), nullable=False),
        pa.field("client_time_ms", pa.float64(), nullable=False),
        pa.field("monotonic_time_us", pa.int64(), nullable=False),
        pa.field("simulation_tick", pa.int64(), nullable=True),
        pa.field("payload_json", pa.string(), nullable=False),
        pa.field("broker_partition", pa.int32(), nullable=False),
        pa.field("broker_offset", pa.int64(), nullable=False),
        pa.field("ingested_at_ms", pa.int64(), nullable=False),
    ]
)


def encode_rows(rows: Iterable[EventRow]) -> bytes:
    table = pa.Table.from_pylist([asdict(row) for row in rows], schema=PARQUET_SCHEMA)
    destination = io.BytesIO()
    pq.write_table(table, destination, compression="zstd", version="2.6")
    return destination.getvalue()
