"""Daily raw-event compaction without raw object deletion."""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import io
import json
from dataclasses import asdict, dataclass
from datetime import UTC, date, datetime, timedelta
from typing import Protocol

import aioboto3
import pyarrow.parquet as pq

from .config import Settings, get_settings
from .consumer.parquet import PARQUET_FIELDS, encode_rows
from .consumer.s3_storage import S3ObjectStorage
from .consumer.s3_writer import IdempotentObjectWriter
from .consumer.types import EventRow


class CompactionStorage(Protocol):
    async def list_keys(self, prefix: str) -> list[str]: ...

    async def get(self, key: str) -> bytes: ...


@dataclass(frozen=True, slots=True)
class CompactionResult:
    key: str
    input_objects: int
    input_rows: int
    output_rows: int
    existed: bool


def _required_int(row: dict[str, object], name: str) -> int:
    value = row[name]
    if isinstance(value, bool) or not isinstance(value, int):
        raise ValueError(f"{name} must be an integer")
    return value


def _required_float(row: dict[str, object], name: str) -> float:
    value = row[name]
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError(f"{name} must be numeric")
    return float(value)


def _required_str(row: dict[str, object], name: str) -> str:
    value = row[name]
    if not isinstance(value, str):
        raise ValueError(f"{name} must be a string")
    return value


def _event_row(row: dict[str, object]) -> EventRow:
    tick_value = row["simulation_tick"]
    if tick_value is not None and (isinstance(tick_value, bool) or not isinstance(tick_value, int)):
        raise ValueError("simulation_tick must be an integer or null")
    return EventRow(
        schema_version=_required_int(row, "schema_version"),
        user_id=_required_str(row, "user_id"),
        game_id=_required_str(row, "game_id"),
        game_type=_required_str(row, "game_type"),
        game_version=_required_str(row, "game_version"),
        chunk_id=_required_str(row, "chunk_id"),
        chunk_sequence=_required_int(row, "chunk_sequence"),
        event_id=_required_str(row, "event_id"),
        event_sequence=_required_int(row, "event_sequence"),
        event_type=_required_str(row, "event_type"),
        event_version=_required_int(row, "event_version"),
        visibility=_required_str(row, "visibility"),
        client_time_ms=_required_float(row, "client_time_ms"),
        monotonic_time_us=_required_int(row, "monotonic_time_us"),
        simulation_tick=tick_value,
        payload_json=_required_str(row, "payload_json"),
        broker_partition=_required_int(row, "broker_partition"),
        broker_offset=_required_int(row, "broker_offset"),
        ingested_at_ms=_required_int(row, "ingested_at_ms"),
    )


async def compact_day(
    *,
    storage: CompactionStorage,
    writer: IdempotentObjectWriter,
    day: date,
    raw_prefix: str = "raw",
    compacted_prefix: str = "compacted",
) -> CompactionResult | None:
    raw_path = f"{raw_prefix.strip('/')}/date={day.isoformat()}/"
    keys = await storage.list_keys(raw_path)
    if not keys:
        return None

    rows: list[dict[str, object]] = []
    for key in keys:
        table = pq.read_table(io.BytesIO(await storage.get(key)))
        if tuple(table.schema.names) != PARQUET_FIELDS:
            raise ValueError(f"raw object {key} does not use the gameplay event schema")
        rows.extend(table.to_pylist())

    deduplicated: dict[str, dict[str, object]] = {}
    for row in rows:
        event_id = _required_str(row, "event_id")
        current = deduplicated.get(event_id)
        position = (
            _required_int(row, "broker_partition"),
            _required_int(row, "broker_offset"),
        )
        if current is None or position < (
            _required_int(current, "broker_partition"),
            _required_int(current, "broker_offset"),
        ):
            deduplicated[event_id] = row
    ordered = sorted(
        deduplicated.values(),
        key=lambda row: (
            _required_int(row, "broker_partition"),
            _required_int(row, "broker_offset"),
            _required_int(row, "event_sequence"),
            _required_str(row, "event_id"),
        ),
    )
    encoded = encode_rows(_event_row(row) for row in ordered)
    manifest = hashlib.sha256("\n".join(keys).encode()).hexdigest()
    key = f"{compacted_prefix.strip('/')}/date={day.isoformat()}/events-{manifest[:20]}.parquet"
    receipt = await writer.write(
        key,
        encoded,
        {
            "source-manifest-sha256": manifest,
            "source-object-count": str(len(keys)),
            "input-row-count": str(len(rows)),
            "output-row-count": str(len(ordered)),
        },
    )
    return CompactionResult(
        key=receipt.key,
        input_objects=len(keys),
        input_rows=len(rows),
        output_rows=len(ordered),
        existed=receipt.existed,
    )


async def run(settings: Settings, day: date) -> CompactionResult | None:
    storage = S3ObjectStorage(
        session=aioboto3.Session(),
        bucket=settings.s3_bucket,
        endpoint_url=settings.s3_endpoint_url,
        region=settings.s3_region,
        access_key_id=settings.s3_access_key_id,
        secret_access_key=settings.s3_secret_access_key,
    )
    return await compact_day(
        storage=storage,
        writer=IdempotentObjectWriter(storage),
        day=day,
        raw_prefix=settings.s3_raw_prefix,
        compacted_prefix=settings.s3_compacted_prefix,
    )


def main() -> None:
    parser = argparse.ArgumentParser(description="Compact one UTC day of raw gameplay events")
    parser.add_argument(
        "--date",
        type=date.fromisoformat,
        default=(datetime.now(UTC) - timedelta(days=1)).date(),
        help="UTC date in YYYY-MM-DD format; defaults to yesterday",
    )
    arguments = parser.parse_args()
    result = asyncio.run(run(get_settings(), arguments.date))
    print(json.dumps(None if result is None else asdict(result), sort_keys=True, default=str))


if __name__ == "__main__":
    main()
