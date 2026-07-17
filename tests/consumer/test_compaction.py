import io
from datetime import date

import pyarrow.parquet as pq
import pytest

from retro_backend.compaction import compact_day
from retro_backend.consumer.parquet import PARQUET_FIELDS, encode_rows
from retro_backend.consumer.s3_writer import IdempotentObjectWriter, ObjectInfo
from tests.consumer.test_parquet import event_row


class Storage:
    def __init__(self) -> None:
        self.objects: dict[str, tuple[bytes, dict[str, str]]] = {}

    async def head(self, key: str) -> ObjectInfo | None:
        value = self.objects.get(key)
        if value is None:
            return None
        return ObjectInfo(len(value[0]), value[1])

    async def put(self, key: str, body: bytes, metadata: dict[str, str]) -> None:
        self.objects[key] = (body, metadata)

    async def get(self, key: str) -> bytes:
        return self.objects[key][0]

    async def list_keys(self, prefix: str) -> list[str]:
        return sorted(key for key in self.objects if key.startswith(prefix))


@pytest.mark.asyncio
async def test_compact_day_deduplicates_event_ids_without_deleting_raw() -> None:
    storage = Storage()
    prefix = "raw/date=2026-07-17/"
    storage.objects[f"{prefix}a.parquet"] = (
        encode_rows([event_row(event_id="evt_duplicate", broker_offset=3)]),
        {},
    )
    storage.objects[f"{prefix}b.parquet"] = (
        encode_rows(
            [
                event_row(event_id="evt_duplicate", broker_offset=8),
                event_row(event_id="evt_unique", broker_offset=8),
            ]
        ),
        {},
    )
    raw_keys = set(storage.objects)

    result = await compact_day(
        storage=storage,
        writer=IdempotentObjectWriter(storage),
        day=date(2026, 7, 17),
        raw_prefix="raw",
        compacted_prefix="compacted",
    )

    assert result and result.input_rows == 3 and result.output_rows == 2
    assert result.key.startswith("compacted/date=2026-07-17/events-")
    table = pq.read_table(io.BytesIO(storage.objects[result.key][0]))
    assert table.schema.names == list(PARQUET_FIELDS)
    assert sorted(table.column("event_id").to_pylist()) == ["evt_duplicate", "evt_unique"]
    duplicate = [row for row in table.to_pylist() if row["event_id"] == "evt_duplicate"]
    assert duplicate[0]["broker_offset"] == 3
    assert raw_keys.issubset(storage.objects)


@pytest.mark.asyncio
async def test_compact_day_is_idempotent_and_empty_days_are_noops() -> None:
    storage = Storage()
    storage.objects["raw/date=2026-07-17/a.parquet"] = (
        encode_rows([event_row()]),
        {},
    )
    writer = IdempotentObjectWriter(storage)

    first = await compact_day(
        storage=storage,
        writer=writer,
        day=date(2026, 7, 17),
    )
    second = await compact_day(
        storage=storage,
        writer=writer,
        day=date(2026, 7, 17),
    )
    empty = await compact_day(
        storage=storage,
        writer=writer,
        day=date(2026, 7, 18),
    )

    assert first and second and first.key == second.key
    assert empty is None
