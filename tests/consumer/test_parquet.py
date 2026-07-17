import io

import pyarrow.parquet as pq

from retro_backend.consumer.parquet import PARQUET_FIELDS, encode_rows
from retro_backend.consumer.types import EventRow


def event_row(
    *, event_id: str = "evt_1", event_sequence: int = 1, broker_offset: int = 7
) -> EventRow:
    return EventRow(
        schema_version=2,
        user_id="usr_1",
        game_id="game_1",
        game_type="2048",
        game_version="1",
        chunk_id="chk_1",
        chunk_sequence=1,
        event_id=event_id,
        event_sequence=event_sequence,
        event_type="episode_started",
        event_version=1,
        visibility="policy",
        client_time_ms=1000.5,
        monotonic_time_us=0,
        simulation_tick=None,
        payload_json='{"a":1}',
        broker_partition=2,
        broker_offset=broker_offset,
        ingested_at_ms=1001,
    )


def test_encode_rows_uses_exact_fixed_schema() -> None:
    encoded = encode_rows([event_row()])
    table = pq.read_table(io.BytesIO(encoded))

    assert table.schema.names == list(PARQUET_FIELDS)
    assert table.to_pylist()[0]["simulation_tick"] is None
    assert "score" not in table.schema.names
    assert "auth_token" not in table.schema.names


def test_encode_rows_is_deterministic_for_identical_rows() -> None:
    rows = [event_row(), event_row(event_id="evt_2", event_sequence=2)]

    assert encode_rows(rows) == encode_rows(rows)
