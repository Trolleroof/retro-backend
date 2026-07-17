import asyncio
import io
import json
from collections.abc import AsyncIterator
from dataclasses import replace
from datetime import date

import pyarrow.parquet as pq
import pytest
from typing_extensions import override

from retro_backend.compaction import compact_day
from retro_backend.consumer.parquet import PARQUET_FIELDS, encode_rows
from retro_backend.consumer.s3_writer import IdempotentObjectWriter, ObjectInfo, ObjectReceipt
from tests.consumer.test_parquet import event_row


class Storage:
    def __init__(self) -> None:
        self.objects: dict[str, tuple[bytes, dict[str, str]]] = {}
        self.put_order: list[str] = []
        self.versions: dict[str, int] = {}

    async def head(self, key: str) -> ObjectInfo | None:
        value = self.objects.get(key)
        if value is None:
            return None
        return ObjectInfo(len(value[0]), value[1])

    async def put(self, key: str, body: bytes, metadata: dict[str, str]) -> None:
        self.put_order.append(key)
        self.objects[key] = (body, metadata)
        self.versions[key] = self.versions.get(key, 0) + 1

    async def get(self, key: str) -> bytes:
        return self.objects[key][0]

    async def get_with_version(self, key: str) -> tuple[bytes, str] | None:
        if key not in self.objects:
            return None
        version = self.versions.setdefault(key, 1)
        return self.objects[key][0], str(version)

    async def put_if_version(
        self,
        key: str,
        body: bytes,
        metadata: dict[str, str],
        expected_version: str | None,
    ) -> bool:
        current = await self.get_with_version(key)
        current_version = current[1] if current is not None else None
        if current_version != expected_version:
            return False
        await self.put(key, body, metadata)
        return True

    async def iter_keys(self, prefix: str) -> AsyncIterator[str]:
        for key in sorted(key for key in self.objects if key.startswith(prefix)):
            yield key


def manifest(storage: Storage, key: str) -> dict[str, object]:
    return json.loads(storage.objects[key][0])


def compacted_rows(storage: Storage, manifest_key: str) -> list[dict[str, object]]:
    result: list[dict[str, object]] = []
    document = manifest(storage, manifest_key)
    parts = document["parts"]
    assert isinstance(parts, list)
    for part in parts:
        assert isinstance(part, dict)
        table = pq.read_table(io.BytesIO(storage.objects[str(part["key"])][0]))
        assert table.schema.names == list(PARQUET_FIELDS)
        result.extend(table.to_pylist())
    return result


@pytest.mark.asyncio
async def test_compact_day_deduplicates_by_user_and_event_without_deleting_raw() -> None:
    storage = Storage()
    prefix = "raw/date=2026-07-17/"
    storage.objects[f"{prefix}a.parquet"] = (
        encode_rows(
            [replace(event_row(event_id="evt_same", broker_offset=3), user_id="usr_a")]
        ),
        {},
    )
    storage.objects[f"{prefix}b.parquet"] = (
        encode_rows(
            [
                replace(event_row(event_id="evt_same", broker_offset=8), user_id="usr_a"),
                replace(event_row(event_id="evt_same", broker_offset=8), user_id="usr_b"),
            ]
        ),
        {},
    )
    raw_keys = set(storage.objects)

    result = await compact_day(
        storage=storage,
        writer=IdempotentObjectWriter(storage),
        day=date(2026, 7, 17),
        part_max_rows=100,
    )

    assert result and result.input_rows == 3 and result.output_rows == 2
    assert result.manifest_key == "compacted/date=2026-07-17/latest.json"
    rows = compacted_rows(storage, result.manifest_key)
    assert {(row["user_id"], row["event_id"]) for row in rows} == {
        ("usr_a", "evt_same"),
        ("usr_b", "evt_same"),
    }
    user_a = [row for row in rows if row["user_id"] == "usr_a"]
    assert user_a[0]["broker_offset"] == 3
    assert raw_keys.issubset(storage.objects)


@pytest.mark.asyncio
async def test_late_data_rerun_publishes_new_generation_and_keeps_prior_generation() -> None:
    storage = Storage()
    raw_prefix = "raw/date=2026-07-17/"
    storage.objects[f"{raw_prefix}a.parquet"] = (encode_rows([event_row()]), {})
    writer = IdempotentObjectWriter(storage)

    first = await compact_day(storage=storage, writer=writer, day=date(2026, 7, 17))
    assert first
    first_parts = set(first.part_keys)
    first_generation = first.generation

    storage.objects[f"{raw_prefix}b.parquet"] = (
        encode_rows(
            [replace(event_row(event_id="evt_late", broker_offset=9), user_id="usr_late")]
        ),
        {},
    )
    second = await compact_day(storage=storage, writer=writer, day=date(2026, 7, 17))

    assert second and second.generation != first_generation
    assert set(second.part_keys).isdisjoint(first_parts)
    assert first_parts.issubset(storage.objects)
    latest = manifest(storage, second.manifest_key)
    assert latest["generation"] == second.generation
    assert len(compacted_rows(storage, second.manifest_key)) == 2


@pytest.mark.asyncio
async def test_compaction_writes_bounded_parts_then_latest_manifest_last() -> None:
    storage = Storage()
    prefix = "raw/date=2026-07-17/"
    for offset in range(5):
        storage.objects[f"{prefix}{offset}.parquet"] = (
            encode_rows([event_row(event_id=f"evt_{offset}", broker_offset=offset)]),
            {},
        )

    result = await compact_day(
        storage=storage,
        writer=IdempotentObjectWriter(storage),
        day=date(2026, 7, 17),
        part_max_rows=2,
        read_batch_rows=1,
    )

    assert result and len(result.part_keys) == 3
    latest = manifest(storage, result.manifest_key)
    parts = latest["parts"]
    assert isinstance(parts, list)
    assert [part["row_count"] for part in parts if isinstance(part, dict)] == [2, 2, 1]
    assert storage.put_order[-1] == result.manifest_key
    assert all(key in storage.objects for key in result.part_keys)


@pytest.mark.asyncio
async def test_compact_day_is_idempotent_and_empty_days_are_noops() -> None:
    storage = Storage()
    storage.objects["raw/date=2026-07-17/a.parquet"] = (encode_rows([event_row()]), {})
    writer = IdempotentObjectWriter(storage)

    first = await compact_day(storage=storage, writer=writer, day=date(2026, 7, 17))
    second = await compact_day(storage=storage, writer=writer, day=date(2026, 7, 17))
    empty = await compact_day(storage=storage, writer=writer, day=date(2026, 7, 18))

    assert first and second and first.generation == second.generation
    assert first.part_keys == second.part_keys
    assert empty is None


@pytest.mark.asyncio
async def test_failed_generation_never_replaces_latest_manifest() -> None:
    storage = Storage()
    prefix = "raw/date=2026-07-17/"
    storage.objects[f"{prefix}a.parquet"] = (encode_rows([event_row()]), {})
    writer = IdempotentObjectWriter(storage)
    first = await compact_day(storage=storage, writer=writer, day=date(2026, 7, 17))
    assert first
    previous_manifest = storage.objects[first.manifest_key]

    storage.objects[f"{prefix}b.parquet"] = (
        encode_rows([event_row(event_id="evt_late", broker_offset=9)]),
        {},
    )

    class FailingWriter:
        async def write(
            self,
            key: str,
            body: bytes,
            metadata: dict[str, str] | None = None,
        ) -> ObjectReceipt:
            _ = (key, body, metadata)
            raise OSError("part upload failed")

    with pytest.raises(OSError, match="part upload failed"):
        await compact_day(
            storage=storage,
            writer=FailingWriter(),
            day=date(2026, 7, 17),
        )

    assert storage.objects[first.manifest_key] == previous_manifest


@pytest.mark.asyncio
async def test_older_compaction_cannot_overwrite_newer_latest_manifest() -> None:
    class InterleavingStorage(Storage):
        def __init__(self) -> None:
            super().__init__()
            self.old_publish_blocked = asyncio.Event()
            self.release_old_publish = asyncio.Event()
            self.blocked_once = False

        @override
        async def put_if_version(
            self,
            key: str,
            body: bytes,
            metadata: dict[str, str],
            expected_version: str | None,
        ) -> bool:
            document = json.loads(body)
            if document["source_object_count"] == 1 and not self.blocked_once:
                self.blocked_once = True
                self.old_publish_blocked.set()
                await self.release_old_publish.wait()
            return await super().put_if_version(
                key,
                body,
                metadata,
                expected_version,
            )

    storage = InterleavingStorage()
    prefix = "raw/date=2026-07-17/"
    storage.objects[f"{prefix}a.parquet"] = (encode_rows([event_row()]), {})
    writer = IdempotentObjectWriter(storage)

    older_task = asyncio.create_task(
        compact_day(storage=storage, writer=writer, day=date(2026, 7, 17))
    )
    await asyncio.wait_for(storage.old_publish_blocked.wait(), timeout=0.1)
    storage.objects[f"{prefix}b.parquet"] = (
        encode_rows([event_row(event_id="evt_late", broker_offset=9)]),
        {},
    )

    newer = await compact_day(storage=storage, writer=writer, day=date(2026, 7, 17))
    storage.release_old_publish.set()
    older = await older_task

    assert newer and newer.published
    assert older and not older.published
    latest = manifest(storage, newer.manifest_key)
    assert latest["generation"] == newer.generation
    assert latest["source_object_count"] == 2
