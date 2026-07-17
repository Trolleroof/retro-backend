"""Bounded daily compaction with immutable generations and an atomic latest pointer."""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import io
import json
import sqlite3
import tempfile
from collections.abc import AsyncIterator
from dataclasses import asdict, dataclass
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from typing import Protocol

import aioboto3
import pyarrow.parquet as pq

from .config import Settings, get_settings
from .consumer.parquet import PARQUET_FIELDS, encode_rows
from .consumer.s3_storage import S3ObjectStorage
from .consumer.s3_writer import IdempotentObjectWriter, ObjectReceipt
from .consumer.types import EventRow


class CompactionStorage(Protocol):
    def iter_keys(self, prefix: str) -> AsyncIterator[str]: ...

    async def get(self, key: str) -> bytes: ...

    async def put(self, key: str, body: bytes, metadata: dict[str, str]) -> None: ...


class CompactionWriter(Protocol):
    async def write(
        self, key: str, body: bytes, metadata: dict[str, str] | None = None
    ) -> ObjectReceipt: ...


@dataclass(frozen=True, slots=True)
class CompactionResult:
    manifest_key: str
    generation: str
    part_keys: tuple[str, ...]
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


def _create_index(connection: sqlite3.Connection) -> None:
    connection.execute(
        """
        CREATE TABLE events (
            user_id TEXT NOT NULL,
            event_id TEXT NOT NULL,
            broker_partition INTEGER NOT NULL,
            broker_offset INTEGER NOT NULL,
            event_sequence INTEGER NOT NULL,
            row_json TEXT NOT NULL,
            PRIMARY KEY (user_id, event_id)
        ) WITHOUT ROWID
        """
    )


def _index_row(connection: sqlite3.Connection, row: dict[str, object]) -> None:
    validated = _event_row(row)
    encoded = json.dumps(asdict(validated), sort_keys=True, separators=(",", ":"), allow_nan=False)
    connection.execute(
        """
        INSERT INTO events (
            user_id, event_id, broker_partition, broker_offset, event_sequence, row_json
        ) VALUES (?, ?, ?, ?, ?, ?)
        ON CONFLICT (user_id, event_id) DO UPDATE SET
            broker_partition = excluded.broker_partition,
            broker_offset = excluded.broker_offset,
            event_sequence = excluded.event_sequence,
            row_json = excluded.row_json
        WHERE excluded.broker_partition < events.broker_partition
           OR (
                excluded.broker_partition = events.broker_partition
                AND excluded.broker_offset < events.broker_offset
           )
        """,
        (
            validated.user_id,
            validated.event_id,
            validated.broker_partition,
            validated.broker_offset,
            validated.event_sequence,
            encoded,
        ),
    )


def _manifest_bytes(document: dict[str, object]) -> bytes:
    return json.dumps(document, sort_keys=True, separators=(",", ":")).encode()


async def compact_day(
    *,
    storage: CompactionStorage,
    writer: CompactionWriter,
    day: date,
    raw_prefix: str = "raw",
    compacted_prefix: str = "compacted",
    read_batch_rows: int = 1_000,
    part_max_rows: int = 500,
) -> CompactionResult | None:
    if read_batch_rows <= 0 or part_max_rows <= 0:
        raise ValueError("compaction row bounds must be positive")

    raw_path = f"{raw_prefix.strip('/')}/date={day.isoformat()}/"
    source_hash = hashlib.sha256()
    input_objects = 0
    input_rows = 0

    with tempfile.TemporaryDirectory(prefix="retro-compaction-") as directory:
        connection = sqlite3.connect(Path(directory) / "events.sqlite3")
        try:
            _create_index(connection)
            async for key in storage.iter_keys(raw_path):
                input_objects += 1
                key_bytes = key.encode()
                source_hash.update(len(key_bytes).to_bytes(8, "big"))
                source_hash.update(key_bytes)
                parquet_file = pq.ParquetFile(io.BytesIO(await storage.get(key)))
                if tuple(parquet_file.schema_arrow.names) != PARQUET_FIELDS:
                    raise ValueError(f"raw object {key} does not use the gameplay event schema")
                for batch in parquet_file.iter_batches(batch_size=read_batch_rows):
                    for row in batch.to_pylist():
                        _index_row(connection, row)
                        input_rows += 1
                connection.commit()

            if input_objects == 0:
                return None

            generation = source_hash.hexdigest()
            generation_prefix = (
                f"{compacted_prefix.strip('/')}/date={day.isoformat()}/"
                f"generations/{generation}"
            )
            cursor = connection.execute(
                """
                SELECT row_json FROM events
                ORDER BY broker_partition, broker_offset, event_sequence, user_id, event_id
                """
            )
            part_documents: list[dict[str, object]] = []
            all_parts_existed = True
            output_rows = 0
            part_number = 0
            while serialized_rows := cursor.fetchmany(part_max_rows):
                rows = [_event_row(json.loads(value[0])) for value in serialized_rows]
                part_key = f"{generation_prefix}/part-{part_number:05d}.parquet"
                receipt = await writer.write(
                    part_key,
                    encode_rows(rows),
                    {
                        "generation": generation,
                        "part-number": str(part_number),
                        "row-count": str(len(rows)),
                    },
                )
                part_documents.append(
                    {
                        "key": receipt.key,
                        "row_count": len(rows),
                        "sha256": receipt.sha256,
                        "size": receipt.size,
                    }
                )
                all_parts_existed = all_parts_existed and receipt.existed
                output_rows += len(rows)
                part_number += 1

            manifest_key = f"{compacted_prefix.strip('/')}/date={day.isoformat()}/latest.json"
            manifest = _manifest_bytes(
                {
                    "schema_version": 1,
                    "date": day.isoformat(),
                    "generation": generation,
                    "source_object_count": input_objects,
                    "input_row_count": input_rows,
                    "output_row_count": output_rows,
                    "parts": part_documents,
                }
            )
            manifest_digest = hashlib.sha256(manifest).hexdigest()
            # S3 PUT is atomic at the object level. Publishing this mutable pointer
            # last makes all listed immutable parts authoritative as one generation.
            await storage.put(
                manifest_key,
                manifest,
                {"sha256": manifest_digest, "generation": generation},
            )
            return CompactionResult(
                manifest_key=manifest_key,
                generation=generation,
                part_keys=tuple(str(value["key"]) for value in part_documents),
                input_objects=input_objects,
                input_rows=input_rows,
                output_rows=output_rows,
                existed=all_parts_existed,
            )
        finally:
            connection.close()


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
        read_batch_rows=settings.compaction_read_batch_rows,
        part_max_rows=settings.compaction_part_max_rows,
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
