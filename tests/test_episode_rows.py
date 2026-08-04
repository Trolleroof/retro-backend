import hashlib
import io
import json
from pathlib import Path
from typing import Any

import pytest
from episode_fixtures import (  # pyright: ignore[reportImplicitRelativeImport]
    GAME_ID,
    GAME_TYPE,
    USER_ID,
    seed_chunks,
    three_chunk_episode,
    validated_key,
)

from retro_backend.contracts import EpisodeReady
from retro_backend.episode_rows import (
    EPISODE_COLUMNS,
    EpisodeConversionError,
    build_rows,
    canonical_json,
    episode_id_for,
    load_episode_rows,
    require_game_type,
    sample_to_row,
    utc_date_parts,
    validated_chunk_key,
)
from retro_backend.parquet import write_parquet
from retro_backend.services.bc_export import extract_behavior_cloning_samples
from retro_backend.storage.r2 import R2Store

ROOT = Path(__file__).resolve().parents[1]
GOLDEN = json.loads((ROOT / "tests/fixtures/bc_export_golden.json").read_text())
GOLDEN_CASES = {name: case for name, case in GOLDEN.items() if not name.startswith("__")}
EXPECTED_EPISODE_ID = hashlib.sha256(f"{USER_ID}:{GAME_ID}".encode()).hexdigest()
COLUMN_NAMES = [column.name for column in EPISODE_COLUMNS]


def _store(fake_env: Any) -> R2Store:
    return R2Store(fake_env.RAW_BUCKET)


def _flatten(row: tuple[object, ...]) -> dict[str, Any]:
    return dict(zip(COLUMN_NAMES, row, strict=True))


def test_column_order_and_nullability_match_the_documented_schema() -> None:
    assert [
        (column.name, column.type, column.optional) for column in EPISODE_COLUMNS
    ] == [
        ("schema_version", "INT32", False),
        ("user_id", "UTF8", False),
        ("game_id", "UTF8", False),
        ("game_type", "UTF8", False),
        ("game_version", "UTF8", False),
        ("ruleset_id", "UTF8", True),
        ("event_sequence", "INT64", False),
        ("client_time_ms", "INT64", False),
        ("control_event_id", "UTF8", False),
        ("observation_id", "UTF8", False),
        ("observation_json", "UTF8", False),
        ("action", "UTF8", False),
        ("actor", "UTF8", False),
        ("reward", "DOUBLE", True),
        ("score_delta", "INT64", True),
        ("terminal", "BOOLEAN", True),
        ("moved", "BOOLEAN", True),
    ]


def test_episode_id_is_the_assembler_object_name() -> None:
    assert episode_id_for(USER_ID, GAME_ID) == EXPECTED_EPISODE_ID


def test_utc_date_parts_are_zero_padded() -> None:
    assert utc_date_parts(1_768_564_800_000) == ("2026", "01", "16")


def test_validated_chunk_key_matches_the_gameplay_consumer_layout() -> None:
    assert validated_chunk_key(
        user_id=USER_ID,
        game_id=GAME_ID,
        chunk_id="chk_01",
        created_at_ms=1_768_564_800_000,
    ) == f"validated/date=2026-01-16/user={USER_ID}/game={GAME_ID}/chunk=chk_01.json"


def test_game_type_that_cannot_travel_in_a_key_is_rejected() -> None:
    assert require_game_type(GAME_TYPE) == GAME_TYPE
    with pytest.raises(ValueError, match="invalid format"):
        require_game_type("../escape")


@pytest.mark.parametrize("name", sorted(GOLDEN_CASES))
def test_rows_equal_the_bc_export_golden_output_exactly(name: str) -> None:
    """The +/-0 row-count criterion: the rows are the ported join's samples."""
    case = GOLDEN_CASES[name]
    expected = case["output_default"]

    rows = build_rows(case["input"])

    assert list(rows.samples) == expected
    assert len(rows.rows) == len(expected)
    assert rows.rows == tuple(sample_to_row(sample) for sample in expected)
    for row, sample in zip(rows.rows, expected, strict=True):
        flattened = _flatten(row)
        assert flattened["observation_json"] == canonical_json(sample["observation"])
        assert json.loads(flattened["observation_json"]) == sample["observation"]
        assert flattened["reward"] == (
            None if sample["reward"] is None else float(sample["reward"])
        )
        for field in ("score_delta", "terminal", "moved", "ruleset_id", "action"):
            assert flattened[field] == sample[field]


@pytest.mark.parametrize("name", sorted(GOLDEN_CASES))
def test_golden_rows_round_trip_through_pyarrow(name: str) -> None:
    parquet = pytest.importorskip("pyarrow.parquet")
    case = GOLDEN_CASES[name]
    expected = case["output_default"]

    rows = build_rows(case["input"])
    table = parquet.read_table(io.BytesIO(write_parquet(EPISODE_COLUMNS, rows.parquet_rows)))

    assert table.num_rows == len(expected)
    assert table.schema.names == COLUMN_NAMES
    read = table.to_pylist()
    for actual, sample in zip(read, expected, strict=True):
        assert json.loads(actual["observation_json"]) == sample["observation"]
        assert actual["control_event_id"] == sample["control_event_id"]
        assert actual["terminal"] == sample["terminal"]
        assert actual["moved"] == sample["moved"]
        assert actual["score_delta"] == sample["score_delta"]


def test_nullable_columns_stay_null_rather_than_zero_filled() -> None:
    # `missing_outcome` is the case where a control has no outcome_changed.
    rows = build_rows(GOLDEN_CASES["missing_outcome"]["input"])

    [row] = rows.rows
    flattened = _flatten(row)
    assert flattened["reward"] is None
    assert flattened["score_delta"] is None
    assert flattened["terminal"] is None
    assert flattened["moved"] is None


def test_a_sample_that_violates_the_contract_is_reported_not_coerced() -> None:
    with pytest.raises(EpisodeConversionError, match="score_delta"):
        sample_to_row(
            {
                "schema_version": 1,
                "user_id": "usr_1",
                "game_id": "game_1",
                "game_type": "2048",
                "game_version": "1",
                "ruleset_id": None,
                "event_sequence": 1,
                "client_time_ms": 1,
                "control_event_id": "evt_1",
                "observation_id": "obs_1",
                "observation": {},
                "action": "left",
                "actor": "human",
                "reward": None,
                "score_delta": 1.5,
                "terminal": None,
                "moved": None,
            }
        )


def test_malformed_chunk_input_raises_a_conversion_error() -> None:
    with pytest.raises(EpisodeConversionError, match="BC join"):
        build_rows([{"schema_version": 2, "events": [{"no_event_id": True}]}])


async def test_complete_episode_reads_every_chunk_and_joins_across_them(
    fake_env: Any,
) -> None:
    envelopes, message = three_chunk_episode()
    seed_chunks(fake_env.RAW_BUCKET, envelopes)

    rows = await load_episode_rows(
        _store(fake_env), EpisodeReady.model_validate(message)
    )

    assert rows.missing_chunk_ids == ()
    assert len(rows.source_chunk_ids) == 3
    # Five human control_changed events, each with a moved outcome.
    assert len(rows.rows) == 5
    assert list(rows.samples) == extract_behavior_cloning_samples(envelopes)
    flattened = [_flatten(row) for row in rows.rows]
    assert [row["action"] for row in flattened] == ["left", "right", "up", "down", "left"]
    assert [row["event_sequence"] for row in flattened] == [3, 6, 9, 12, 15]
    assert {row["ruleset_id"] for row in flattened} == {"classic"}
    assert all(row["moved"] is True for row in flattened)
    assert json.loads(flattened[0]["observation_json"]) == {
        "representation": "engine_state_v1",
        "state": {"board": [2, 0, 0, 0]},
    }


@pytest.mark.parametrize("status", ["incomplete", "abandoned"])
async def test_gappy_episodes_convert_without_raising(
    fake_env: Any,
    status: str,
) -> None:
    envelopes, message = three_chunk_episode(
        status=status,
        ended=status != "abandoned",
    )
    # The middle chunk's validated artifact never landed — the gap that makes
    # this episode incomplete in the first place.
    seed_chunks(fake_env.RAW_BUCKET, [envelopes[0], envelopes[2]])

    rows = await load_episode_rows(
        _store(fake_env), EpisodeReady.model_validate(message)
    )

    assert rows.missing_chunk_ids == (envelopes[1]["chunk_id"],)
    assert rows.source_chunk_ids == (envelopes[0]["chunk_id"], envelopes[2]["chunk_id"])
    assert len(rows.rows) < 5


async def test_r2_read_failure_propagates_for_queue_retry(fake_env: Any) -> None:
    envelopes, message = three_chunk_episode()
    seed_chunks(fake_env.RAW_BUCKET, envelopes)
    fake_env.RAW_BUCKET.get_failures[validated_key(envelopes[1])] = RuntimeError(
        "R2 unavailable"
    )

    with pytest.raises(RuntimeError, match="R2 unavailable"):
        await load_episode_rows(
            _store(fake_env), EpisodeReady.model_validate(message)
        )


async def test_corrupt_artifact_is_treated_as_a_missing_chunk(fake_env: Any) -> None:
    envelopes, message = three_chunk_episode()
    seed_chunks(fake_env.RAW_BUCKET, envelopes)
    fake_env.RAW_BUCKET.seed_raw(validated_key(envelopes[2]), b"not json", "0" * 64)

    rows = await load_episode_rows(
        _store(fake_env), EpisodeReady.model_validate(message)
    )

    assert rows.missing_chunk_ids == (envelopes[2]["chunk_id"],)
    assert len(rows.source_chunk_ids) == 2
