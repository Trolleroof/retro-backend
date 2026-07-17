import pytest

from retro_backend.consumer.buffer import OffsetGapError, PartitionBuffer
from retro_backend.consumer.types import BrokerRecord
from tests.consumer.test_parquet import event_row


def record(offset: int, *, size: int = 8) -> BrokerRecord:
    return BrokerRecord(
        topic="events",
        partition=1,
        offset=offset,
        timestamp_ms=1_700_000_000_000,
        key=None,
        value=b"x" * size,
        headers=(),
    )


def test_partition_buffer_accepts_only_contiguous_offsets_and_preserves_range() -> None:
    buffer = PartitionBuffer(max_records=3, max_rows=10, max_bytes=100)

    assert not buffer.add(record(10), [event_row(broker_offset=10)])
    assert not buffer.add(record(11), [event_row(event_id="evt_2", broker_offset=11)])
    with pytest.raises(OffsetGapError, match="expected offset 12"):
        buffer.add(record(13), [event_row(event_id="evt_3", broker_offset=13)])

    batch = buffer.batch()
    assert (batch.start_offset, batch.end_offset) == (10, 11)
    assert batch.record_count == 2


@pytest.mark.parametrize(
    ("buffer", "rows", "size"),
    [
        (PartitionBuffer(max_records=1, max_rows=10, max_bytes=100), 1, 8),
        (PartitionBuffer(max_records=10, max_rows=2, max_bytes=100), 2, 8),
        (PartitionBuffer(max_records=10, max_rows=10, max_bytes=8), 1, 8),
    ],
)
def test_partition_buffer_reports_bounded_batch_thresholds(
    buffer: PartitionBuffer, rows: int, size: int
) -> None:
    values = [event_row(event_id=f"evt_{index}") for index in range(rows)]

    assert buffer.add(record(0, size=size), values)
