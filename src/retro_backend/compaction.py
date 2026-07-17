"""Bounded daily compaction with immutable generations and an atomic latest pointer."""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import io
import json
import sqlite3
import tempfile
from collections.abc import AsyncGenerator, AsyncIterator, Awaitable, Callable
from dataclasses import asdict, dataclass
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from typing import Protocol, cast

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

    async def get_with_version(self, key: str) -> tuple[bytes, str] | None: ...

    def iter_lines(self, key: str) -> AsyncIterator[bytes]: ...

    async def put_if_version(
        self,
        key: str,
        body: bytes,
        metadata: dict[str, str],
        expected_version: str | None,
    ) -> bool: ...


class CompactionWriter(Protocol):
    async def write(
        self, key: str, body: bytes, metadata: dict[str, str] | None = None
    ) -> ObjectReceipt: ...

    async def write_file(
        self,
        key: str,
        path: Path,
        metadata: dict[str, str] | None = None,
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
    published: bool


@dataclass(frozen=True, slots=True)
class SourceIndex:
    object_count: int
    generation: str
    sha256: str


@dataclass(frozen=True, slots=True)
class LatestSnapshot:
    document: dict[str, object]
    version: str


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


def _manifest_int(document: dict[str, object], name: str) -> int:
    value = document.get(name)
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise ValueError(f"latest compaction manifest has invalid {name}")
    return value


def _manifest_str(document: dict[str, object], name: str) -> str:
    value = document.get(name)
    if not isinstance(value, str) or not value:
        raise ValueError(f"latest compaction manifest has invalid {name}")
    return value


def _manifest_schema(document: dict[str, object]) -> int:
    schema = _manifest_int(document, "schema_version")
    if schema not in {1, 2}:
        raise ValueError(f"unsupported latest compaction manifest schema {schema}")
    return schema


def _source_key_line(key: str) -> bytes:
    return json.dumps(key, ensure_ascii=False, separators=(",", ":")).encode() + b"\n"


def _v1_source_keys(document: dict[str, object]) -> list[str] | None:
    if "source_keys" not in document:
        return None
    value = document["source_keys"]
    if not isinstance(value, list) or any(not isinstance(key, str) or not key for key in value):
        raise ValueError("v1 latest compaction manifest has invalid source_keys")
    keys = cast(list[str], value)
    if len(keys) != _manifest_int(document, "source_object_count"):
        raise ValueError("v1 latest compaction manifest source_keys count does not match")
    previous_key: str | None = None
    for key in keys:
        if previous_key is not None and key <= previous_key:
            raise ValueError("v1 latest compaction manifest source_keys must be unique and sorted")
        previous_key = key
    return keys


async def build_source_index(
    keys: AsyncIterator[str],
    path: Path,
    process_key: Callable[[str], Awaitable[None]] | None = None,
) -> SourceIndex:
    generation_hash = hashlib.sha256()
    index_hash = hashlib.sha256()
    object_count = 0
    previous_key: str | None = None
    with path.open("wb") as target:
        async for key in keys:
            if not key or (previous_key is not None and key <= previous_key):
                raise ValueError("raw source keys must be listed in unique lexicographic order")
            key_bytes = key.encode()
            generation_hash.update(len(key_bytes).to_bytes(8, "big"))
            generation_hash.update(key_bytes)
            line = _source_key_line(key)
            target.write(line)
            index_hash.update(line)
            object_count += 1
            previous_key = key
            if process_key is not None:
                await process_key(key)
    return SourceIndex(
        object_count=object_count,
        generation=generation_hash.hexdigest(),
        sha256=index_hash.hexdigest(),
    )


async def _read_latest(storage: CompactionStorage, key: str) -> LatestSnapshot | None:
    stored = await storage.get_with_version(key)
    if stored is None:
        return None
    body, version = stored
    document = json.loads(body)
    if not isinstance(document, dict):
        raise ValueError("latest compaction manifest must be an object")
    if _manifest_schema(document) == 1:
        _v1_source_keys(document)
    return LatestSnapshot(document, version)


async def _validated_source_keys(
    lines: AsyncIterator[bytes],
    *,
    expected_count: int,
    expected_sha256: str,
) -> AsyncGenerator[str, None]:
    digest = hashlib.sha256()
    count = 0
    previous_key: str | None = None
    async for encoded_line in lines:
        if not encoded_line.endswith(b"\n"):
            raise ValueError("source index contains an unterminated final line")
        encoded_key = encoded_line[:-1]
        try:
            key = json.loads(encoded_key)
        except (UnicodeDecodeError, json.JSONDecodeError) as error:
            raise ValueError("source index contains invalid NDJSON") from error
        if not isinstance(key, str) or not key or encoded_line != _source_key_line(key):
            raise ValueError("source index contains a noncanonical key")
        if previous_key is not None and key <= previous_key:
            raise ValueError("source index keys must be unique and sorted")
        digest.update(encoded_line)
        count += 1
        previous_key = key
        yield key
    if count != expected_count or digest.hexdigest() != expected_sha256:
        raise ValueError("source index count or checksum does not match latest manifest")


async def _iter_v1_source_keys(keys: list[str]) -> AsyncGenerator[str, None]:
    for key in keys:
        yield key


async def _candidate_contains_source_keys(
    storage: CompactionStorage,
    *,
    candidate: dict[str, object],
    current_keys: AsyncGenerator[str, None],
    current_count: int,
    strict: bool,
) -> bool:
    candidate_count = _manifest_int(candidate, "source_object_count")
    if candidate_count < current_count or (strict and candidate_count == current_count):
        return False
    candidate_keys = _validated_source_keys(
        storage.iter_lines(_manifest_str(candidate, "source_index_key")),
        expected_count=candidate_count,
        expected_sha256=_manifest_str(candidate, "source_index_sha256"),
    )
    try:
        candidate_key = await anext(candidate_keys, None)
        async for current_key in current_keys:
            while candidate_key is not None and candidate_key < current_key:
                candidate_key = await anext(candidate_keys, None)
            if candidate_key != current_key:
                return False
        async for _ in candidate_keys:
            pass
        return True
    finally:
        await current_keys.aclose()
        await candidate_keys.aclose()


async def _candidate_is_strict_superset(
    storage: CompactionStorage,
    *,
    current: dict[str, object],
    candidate: dict[str, object],
) -> bool:
    current_count = _manifest_int(current, "source_object_count")
    return await _candidate_contains_source_keys(
        storage,
        candidate=candidate,
        current_keys=_validated_source_keys(
            storage.iter_lines(_manifest_str(current, "source_index_key")),
            expected_count=current_count,
            expected_sha256=_manifest_str(current, "source_index_sha256"),
        ),
        current_count=current_count,
        strict=True,
    )


async def _publish_latest(
    *,
    storage: CompactionStorage,
    key: str,
    body: bytes,
    metadata: dict[str, str],
    candidate: dict[str, object],
    baseline: LatestSnapshot | None,
) -> bool:
    candidate_generation = candidate["generation"]
    if baseline is not None and _manifest_schema(baseline.document) == 1:
        current = await _read_latest(storage, key)
        if current is None or current.version != baseline.version:
            return False
        source_keys = _v1_source_keys(baseline.document)
        if source_keys is not None:
            if not await _candidate_contains_source_keys(
                storage,
                candidate=candidate,
                current_keys=_iter_v1_source_keys(source_keys),
                current_count=len(source_keys),
                strict=False,
            ):
                return False
        elif _manifest_int(candidate, "source_object_count") < _manifest_int(
            baseline.document, "source_object_count"
        ):
            return False
        return await storage.put_if_version(
            key,
            body,
            metadata,
            baseline.version,
        )

    while True:
        current = await _read_latest(storage, key)
        expected_version: str | None = None
        if current is not None:
            current_document = current.document
            expected_version = current.version
            if _manifest_schema(current_document) == 1:
                return False
            if current_document.get("generation") == candidate_generation:
                return False
            if not await _candidate_is_strict_superset(
                storage,
                current=current_document,
                candidate=candidate,
            ):
                return False
        if await storage.put_if_version(
            key,
            body,
            metadata,
            expected_version,
        ):
            return True


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
    manifest_key = f"{compacted_prefix.strip('/')}/date={day.isoformat()}/latest.json"
    baseline = await _read_latest(storage, manifest_key)
    input_rows = 0

    with tempfile.TemporaryDirectory(prefix="retro-compaction-") as directory:
        connection = sqlite3.connect(Path(directory) / "events.sqlite3")
        try:
            _create_index(connection)

            async def process_raw_object(key: str) -> None:
                nonlocal input_rows
                parquet_file = pq.ParquetFile(io.BytesIO(await storage.get(key)))
                if tuple(parquet_file.schema_arrow.names) != PARQUET_FIELDS:
                    raise ValueError(f"raw object {key} does not use the gameplay event schema")
                for batch in parquet_file.iter_batches(batch_size=read_batch_rows):
                    for row in batch.to_pylist():
                        _index_row(connection, row)
                        input_rows += 1
                connection.commit()

            source_path = Path(directory) / "source-keys.ndjson"
            source = await build_source_index(
                storage.iter_keys(raw_path),
                source_path,
                process_raw_object,
            )
            if source.object_count == 0:
                return None

            input_objects = source.object_count
            generation = source.generation
            generation_prefix = (
                f"{compacted_prefix.strip('/')}/date={day.isoformat()}/"
                f"generations/{generation}"
            )
            source_index_key = f"{generation_prefix}/source-keys.ndjson"
            source_receipt = await writer.write_file(
                source_index_key,
                source_path,
                {
                    "generation": generation,
                    "source-object-count": str(input_objects),
                },
            )
            if source_receipt.sha256 != source.sha256:
                raise OSError("uploaded source index checksum does not match local index")
            cursor = connection.execute(
                """
                SELECT row_json FROM events
                ORDER BY broker_partition, broker_offset, event_sequence, user_id, event_id
                """
            )
            part_documents: list[dict[str, object]] = []
            all_parts_existed = source_receipt.existed
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

            manifest_document: dict[str, object] = {
                "schema_version": 2,
                "date": day.isoformat(),
                "generation": generation,
                "source_object_count": input_objects,
                "source_index_key": source_index_key,
                "source_index_sha256": source.sha256,
                "input_row_count": input_rows,
                "output_row_count": output_rows,
                "parts": part_documents,
            }
            manifest = _manifest_bytes(manifest_document)
            manifest_digest = hashlib.sha256(manifest).hexdigest()
            published = await _publish_latest(
                storage=storage,
                key=manifest_key,
                body=manifest,
                metadata={
                    "sha256": manifest_digest,
                    "generation": generation,
                    "source-object-count": str(input_objects),
                },
                candidate=manifest_document,
                baseline=baseline,
            )
            return CompactionResult(
                manifest_key=manifest_key,
                generation=generation,
                part_keys=tuple(str(value["key"]) for value in part_documents),
                input_objects=input_objects,
                input_rows=input_rows,
                output_rows=output_rows,
                existed=all_parts_existed,
                published=published,
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
