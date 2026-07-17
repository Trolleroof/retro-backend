import asyncio
import hashlib
import io
import json
from collections.abc import AsyncIterator
from dataclasses import replace
from datetime import date
from pathlib import Path

import pyarrow.parquet as pq
import pytest
from typing_extensions import override

from retro_backend import compaction
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

    async def put_file(self, key: str, path: Path, metadata: dict[str, str]) -> None:
        await self.put(key, path.read_bytes(), metadata)

    async def get(self, key: str) -> bytes:
        return self.objects[key][0]

    async def get_with_version(self, key: str) -> tuple[bytes, str] | None:
        if key not in self.objects:
            return None
        version = self.versions.setdefault(key, 1)
        return self.objects[key][0], str(version)

    async def iter_lines(self, key: str) -> AsyncIterator[bytes]:
        stream = io.BytesIO(self.objects[key][0])
        while line := stream.readline():
            if not line.endswith(b"\n"):
                raise ValueError("source index has an unterminated final line")
            yield line

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


def old_v1_manifest(
    *,
    source_object_count: int,
    generation: str = "old",
    source_keys: list[str] | None = None,
) -> bytes:
    metric_name = "source_keys_sha256" if source_keys is not None else "manifest_sha256"
    document: dict[str, object] = {
        "schema_version": 1,
        "date": "2026-07-17",
        "generation": generation,
        "source_object_count": source_object_count,
        "source_generation_metric": {
            "object_count": source_object_count,
            metric_name: generation,
        },
        "input_row_count": source_object_count,
        "output_row_count": source_object_count,
        "parts": [],
    }
    if source_keys is not None:
        document["source_keys"] = source_keys
    return json.dumps(
        document,
        sort_keys=True,
        separators=(",", ":"),
    ).encode()


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
    assert latest["schema_version"] == 2
    assert "source_keys" not in latest
    source_index_key = latest["source_index_key"]
    assert isinstance(source_index_key, str)
    source_index, source_metadata = storage.objects[source_index_key]
    expected_keys = [f"{prefix}{offset}.parquet" for offset in range(5)]
    assert source_index.splitlines() == [
        json.dumps(key, ensure_ascii=False, separators=(",", ":")).encode()
        for key in expected_keys
    ]
    assert latest["source_index_sha256"] == hashlib.sha256(source_index).hexdigest()
    assert source_metadata["sha256"] == latest["source_index_sha256"]
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
        async def write_file(
            self,
            key: str,
            path: Path,
            metadata: dict[str, str] | None = None,
        ) -> ObjectReceipt:
            return await writer.write_file(key, path, metadata)

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


@pytest.mark.asyncio
async def test_larger_non_superset_source_snapshot_cannot_replace_latest() -> None:
    storage = Storage()
    prefix = "raw/date=2026-07-17/"
    first_key = f"{prefix}a.parquet"
    shared_key = f"{prefix}b.parquet"
    storage.objects[first_key] = (encode_rows([event_row()]), {})
    storage.objects[shared_key] = (
        encode_rows([event_row(event_id="evt_b", broker_offset=1)]),
        {},
    )
    writer = IdempotentObjectWriter(storage)
    current = await compact_day(storage=storage, writer=writer, day=date(2026, 7, 17))
    assert current and current.published
    previous_latest = storage.objects[current.manifest_key]

    del storage.objects[first_key]
    storage.objects[f"{prefix}c.parquet"] = (
        encode_rows([event_row(event_id="evt_c", broker_offset=2)]),
        {},
    )
    storage.objects[f"{prefix}d.parquet"] = (
        encode_rows([event_row(event_id="evt_d", broker_offset=3)]),
        {},
    )
    candidate = await compact_day(
        storage=storage,
        writer=writer,
        day=date(2026, 7, 17),
    )

    assert candidate and candidate.input_objects == 3
    assert not candidate.published
    assert storage.objects[current.manifest_key] == previous_latest


@pytest.mark.asyncio
async def test_v1_latest_upgrades_once_from_prelisting_baseline() -> None:
    storage = Storage()
    prefix = "raw/date=2026-07-17/"
    storage.objects[f"{prefix}a.parquet"] = (encode_rows([event_row()]), {})
    latest_key = "compacted/date=2026-07-17/latest.json"
    storage.objects[latest_key] = (old_v1_manifest(source_object_count=1), {})
    storage.versions[latest_key] = 7

    result = await compact_day(
        storage=storage,
        writer=IdempotentObjectWriter(storage),
        day=date(2026, 7, 17),
    )

    assert result and result.published
    latest = manifest(storage, latest_key)
    assert latest["schema_version"] == 2
    assert latest["source_object_count"] == 1
    assert "source_index_key" in latest
    assert "source_keys" not in latest


@pytest.mark.asyncio
async def test_v1_upgrade_rejects_pointer_change_during_raw_listing() -> None:
    latest_key = "compacted/date=2026-07-17/latest.json"

    class ConcurrentLatestStorage(Storage):
        @override
        async def iter_keys(self, prefix: str) -> AsyncIterator[str]:
            changed = False
            for key in sorted(key for key in self.objects if key.startswith(prefix)):
                if not changed:
                    self.objects[latest_key] = (
                        old_v1_manifest(source_object_count=2, generation="concurrent"),
                        {},
                    )
                    self.versions[latest_key] += 1
                    changed = True
                yield key

    storage = ConcurrentLatestStorage()
    prefix = "raw/date=2026-07-17/"
    storage.objects[f"{prefix}a.parquet"] = (encode_rows([event_row()]), {})
    storage.objects[latest_key] = (old_v1_manifest(source_object_count=1), {})
    storage.versions[latest_key] = 3

    result = await compact_day(
        storage=storage,
        writer=IdempotentObjectWriter(storage),
        day=date(2026, 7, 17),
    )

    assert result and not result.published
    assert manifest(storage, latest_key)["generation"] == "concurrent"


@pytest.mark.asyncio
async def test_v1_with_keys_rejects_same_count_non_superset() -> None:
    storage = Storage()
    prefix = "raw/date=2026-07-17/"
    old_keys = [f"{prefix}a.parquet", f"{prefix}b.parquet"]
    storage.objects[f"{prefix}a.parquet"] = (encode_rows([event_row()]), {})
    storage.objects[f"{prefix}c.parquet"] = (
        encode_rows([event_row(event_id="evt_c", broker_offset=2)]),
        {},
    )
    latest_key = "compacted/date=2026-07-17/latest.json"
    storage.objects[latest_key] = (
        old_v1_manifest(source_object_count=2, source_keys=old_keys),
        {},
    )
    storage.versions[latest_key] = 4
    previous = storage.objects[latest_key]

    result = await compact_day(
        storage=storage,
        writer=IdempotentObjectWriter(storage),
        day=date(2026, 7, 17),
    )

    assert result and not result.published
    assert storage.objects[latest_key] == previous


@pytest.mark.asyncio
async def test_v1_with_keys_upgrades_true_superset() -> None:
    storage = Storage()
    prefix = "raw/date=2026-07-17/"
    old_keys = [f"{prefix}a.parquet", f"{prefix}b.parquet"]
    for offset, name in enumerate(("a", "b", "c")):
        storage.objects[f"{prefix}{name}.parquet"] = (
            encode_rows([event_row(event_id=f"evt_{name}", broker_offset=offset)]),
            {},
        )
    latest_key = "compacted/date=2026-07-17/latest.json"
    storage.objects[latest_key] = (
        old_v1_manifest(source_object_count=2, source_keys=old_keys),
        {},
    )
    storage.versions[latest_key] = 9

    result = await compact_day(
        storage=storage,
        writer=IdempotentObjectWriter(storage),
        day=date(2026, 7, 17),
    )

    assert result and result.published
    latest = manifest(storage, latest_key)
    assert latest["schema_version"] == 2
    assert latest["source_object_count"] == 3


@pytest.mark.parametrize(
    ("source_keys", "source_object_count"),
    [
        (["raw/b.parquet", "raw/a.parquet"], 2),
        (["raw/a.parquet", "raw/a.parquet"], 2),
        (["raw/a.parquet"], 2),
    ],
)
@pytest.mark.asyncio
async def test_v1_with_keys_validates_order_uniqueness_and_count(
    source_keys: list[str],
    source_object_count: int,
) -> None:
    storage = Storage()
    raw_key = "raw/date=2026-07-17/a.parquet"
    storage.objects[raw_key] = (encode_rows([event_row()]), {})
    latest_key = "compacted/date=2026-07-17/latest.json"
    storage.objects[latest_key] = (
        old_v1_manifest(
            source_object_count=source_object_count,
            source_keys=source_keys,
        ),
        {},
    )

    with pytest.raises(ValueError, match="source_keys"):
        await compact_day(
            storage=storage,
            writer=IdempotentObjectWriter(storage),
            day=date(2026, 7, 17),
        )


@pytest.mark.asyncio
async def test_source_index_missing_final_newline_is_rejected() -> None:
    storage = Storage()
    prefix = "raw/date=2026-07-17/"
    storage.objects[f"{prefix}a.parquet"] = (encode_rows([event_row()]), {})
    writer = IdempotentObjectWriter(storage)
    current = await compact_day(storage=storage, writer=writer, day=date(2026, 7, 17))
    assert current and current.published
    latest = manifest(storage, current.manifest_key)
    source_index_key = latest["source_index_key"]
    assert isinstance(source_index_key, str)
    body, metadata = storage.objects[source_index_key]
    storage.objects[source_index_key] = (body.removesuffix(b"\n"), metadata)
    storage.objects[f"{prefix}b.parquet"] = (
        encode_rows([event_row(event_id="evt_b", broker_offset=1)]),
        {},
    )

    with pytest.raises(ValueError, match="unterminated"):
        await compact_day(storage=storage, writer=writer, day=date(2026, 7, 17))


@pytest.mark.asyncio
async def test_source_index_streams_100k_keys_without_manifest_growth(tmp_path: Path) -> None:
    index_path = tmp_path / "source-keys.ndjson"
    yielded = 0
    observed_sizes: list[int] = []

    async def keys() -> AsyncIterator[str]:
        nonlocal yielded
        for offset in range(100_000):
            if offset and offset % 20_000 == 0:
                observed_sizes.append(index_path.stat().st_size)
            yielded += 1
            yield f"raw/date=2026-07-17/offset-{offset:06d}.parquet"

    source = await compaction.build_source_index(keys(), index_path)

    assert yielded == 100_000
    assert source.object_count == 100_000
    assert observed_sizes == sorted(observed_sizes) and all(observed_sizes)
    assert not hasattr(source, "keys")
    body = index_path.read_bytes()
    assert source.sha256 == hashlib.sha256(body).hexdigest()
    assert body.splitlines()[0] == b'"raw/date=2026-07-17/offset-000000.parquet"'
    assert body.splitlines()[-1] == b'"raw/date=2026-07-17/offset-099999.parquet"'
    latest = json.dumps(
        {
            "schema_version": 2,
            "generation": source.generation,
            "source_index_key": "compacted/generations/hash/source-keys.ndjson",
            "source_index_sha256": source.sha256,
            "source_object_count": source.object_count,
            "parts": [],
            "input_row_count": 0,
            "output_row_count": 0,
        },
        sort_keys=True,
        separators=(",", ":"),
    ).encode()
    assert len(latest) < 1_024
    assert b"offset-000000" not in latest


@pytest.mark.asyncio
async def test_source_index_generation_is_independent_of_async_buffering(tmp_path: Path) -> None:
    source_keys = (
        "raw/date=2026-07-17/a.parquet",
        "raw/date=2026-07-17/b.parquet",
        "raw/date=2026-07-17/c.parquet",
    )

    async def keys(*, yield_between: bool) -> AsyncIterator[str]:
        for key in source_keys:
            if yield_between:
                await asyncio.sleep(0)
            yield key

    buffered_path = tmp_path / "buffered.ndjson"
    yielding_path = tmp_path / "yielding.ndjson"
    buffered = await compaction.build_source_index(keys(yield_between=False), buffered_path)
    yielding = await compaction.build_source_index(keys(yield_between=True), yielding_path)

    assert buffered == yielding
    assert buffered_path.read_bytes() == yielding_path.read_bytes()
