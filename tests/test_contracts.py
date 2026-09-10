import json
import os
from pathlib import Path
from typing import Any, get_args

import pytest
from jsonschema import Draft202012Validator
from pydantic import ValidationError

from retro_backend.contracts import (
    ChunkEnvelope,
    ContractError,
    GameResultRequest,
    GameType,
    IngestAcknowledgment,
)

ROOT = Path(__file__).resolve().parents[1]
V2_SCHEMA = json.loads(
    (ROOT / "contracts/interaction-trace-chunk-v2.schema.json").read_text()
)
V2_VALIDATOR = Draft202012Validator(V2_SCHEMA)


def test_game_type_contract_lists_supported_games() -> None:
    assert get_args(GameType) == (
        "2048",
        "tetris",
        "snake",
        "minesweeper",
        "sokoban",
        "connect4",
        "lightsout",
        "sudoku",
        "restaurant-arena",
    )


def test_game_result_accepts_supported_score_contract() -> None:
    result = GameResultRequest.model_validate(
        {
            "result_id": "res_01",
            "game_id": "game_01",
            "game_type": "2048",
            "score": 32768,
            "completed_at": "2026-07-17T18:42:00Z",
        }
    )

    assert result.score == 32768
    assert result.completed_at.tzinfo is not None


def test_game_result_rejects_extra_fields() -> None:
    with pytest.raises(ValidationError, match="Extra inputs are not permitted"):
        GameResultRequest.model_validate(
            {
                "result_id": "res_01",
                "game_id": "game_01",
                "game_type": "2048",
                "score": 32768,
                "completed_at": "2026-07-17T18:42:00Z",
                "token": "must-not-be-accepted",
            }
        )


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("result_id", "invalid"),
        ("game_id", "invalid"),
        ("game_type", "pong"),
    ],
)
def test_game_result_rejects_invalid_identifiers_and_game_type(
    field: str, value: str
) -> None:
    payload = {
        "result_id": "res_01",
        "game_id": "game_01",
        "game_type": "2048",
        "score": 32768,
        "completed_at": "2026-07-17T18:42:00Z",
    }
    payload[field] = value

    with pytest.raises(ValidationError):
        GameResultRequest.model_validate(payload)


@pytest.mark.parametrize("score", [-1, 2**63, True, 1.0, "1"])
def test_game_result_rejects_out_of_range_or_non_strict_scores(score: object) -> None:
    with pytest.raises(ValidationError):
        GameResultRequest.model_validate(
            {
                "result_id": "res_01",
                "game_id": "game_01",
                "game_type": "2048",
                "score": score,
                "completed_at": "2026-07-17T18:42:00Z",
            }
        )


def test_game_result_requires_timezone_aware_completion_time() -> None:
    with pytest.raises(ValidationError, match="timezone"):
        GameResultRequest.model_validate(
            {
                "result_id": "res_01",
                "game_id": "game_01",
                "game_type": "2048",
                "score": 32768,
                "completed_at": "2026-07-17T18:42:00",
            }
        )


def test_game_result_bounds_key_forming_game_id_suffix() -> None:
    payload = {
        "result_id": "res_01",
        "game_id": f"game_{'a' * 120}",
        "game_type": "2048",
        "score": 32768,
        "completed_at": "2026-07-17T18:42:00Z",
    }

    GameResultRequest.model_validate(payload)
    payload["game_id"] = f"game_{'a' * 121}"

    with pytest.raises(ValidationError):
        GameResultRequest.model_validate(payload)


def test_game_result_bounds_result_id_suffix() -> None:
    payload = {
        "result_id": f"res_{'a' * 120}",
        "game_id": "game_01",
        "game_type": "2048",
        "score": 32768,
        "completed_at": "2026-07-17T18:42:00Z",
    }

    GameResultRequest.model_validate(payload)
    payload["result_id"] = f"res_{'a' * 121}"

    with pytest.raises(ValidationError):
        GameResultRequest.model_validate(payload)


def test_v2_envelope_accepts_current_interaction_trace_fixture(
    fixture_payload: dict[str, Any],
) -> None:
    Draft202012Validator.check_schema(V2_SCHEMA)
    V2_VALIDATOR.validate(fixture_payload)
    envelope = ChunkEnvelope.model_validate(fixture_payload)

    assert envelope.schema_version == 2
    assert envelope.event_count == len(fixture_payload["events"])


def test_v2_continuation_fixture_validates_without_episode_start(
    continuation_fixture_payload: dict[str, Any],
) -> None:
    V2_VALIDATOR.validate(continuation_fixture_payload)

    envelope = ChunkEnvelope.model_validate(continuation_fixture_payload)

    assert envelope.chunk_sequence == 2
    assert envelope.first_event_sequence == 6
    assert envelope.events[0]["event_type"] == "observation"


def test_v2_envelope_preserves_extension_fields(fixture_payload: dict[str, Any]) -> None:
    fixture_payload["extension"] = {"source": "future-client"}

    V2_VALIDATOR.validate(fixture_payload)
    envelope = ChunkEnvelope.model_validate(fixture_payload)

    assert envelope.model_extra == {"extension": {"source": "future-client"}}


def test_v2_schema_and_model_reject_fractional_created_at_ms(
    fixture_payload: dict[str, Any],
) -> None:
    fixture_payload["created_at_ms"] += 0.5

    assert V2_VALIDATOR.is_valid(fixture_payload) is False
    with pytest.raises(ValidationError):
        ChunkEnvelope.model_validate(fixture_payload)


def test_v2_schema_documents_intentional_runtime_integer_strictness() -> None:
    comment = V2_SCHEMA.get("$comment")

    assert isinstance(comment, str)
    assert "integral JSON numbers such as 2.0" in comment
    assert "decoded Python int values" in comment


@pytest.mark.parametrize(
    "path",
    [
        pytest.param(("schema_version",), id="top-schema-version"),
        pytest.param(("chunk_sequence",), id="top-chunk-sequence"),
        pytest.param(("first_event_sequence",), id="top-first-event-sequence"),
        pytest.param(("last_event_sequence",), id="top-last-event-sequence"),
        pytest.param(("event_count",), id="top-event-count"),
        pytest.param(("created_at_ms",), id="top-created-at-ms"),
        pytest.param(("events", 0, "schema_version"), id="event-schema-version"),
        pytest.param(("events", 0, "event_version"), id="event-version"),
        pytest.param(("events", 0, "event_sequence"), id="event-sequence"),
        pytest.param(("events", 0, "monotonic_time_us"), id="event-monotonic-time"),
        pytest.param(("events", 0, "simulation_tick"), id="event-simulation-tick"),
    ],
)
def test_runtime_intentionally_rejects_integral_floats_accepted_by_schema(
    fixture_payload: dict[str, Any],
    path: tuple[str | int, ...],
) -> None:
    target: Any = fixture_payload
    for segment in path[:-1]:
        target = target[segment]
    field = path[-1]
    value = target[field]
    assert isinstance(value, int) and not isinstance(value, bool)
    target[field] = float(value)

    assert V2_VALIDATOR.is_valid(fixture_payload) is True
    with pytest.raises(ValidationError):
        ChunkEnvelope.model_validate(fixture_payload)


def _set_identifier_suffix_length(
    payload: dict[str, Any],
    field: str,
    prefix: str,
    suffix_length: int,
) -> None:
    value = f"{prefix}{'a' * suffix_length}"
    if field == "event_id":
        payload["events"][1][field] = value
        return
    payload[field] = value
    for event in payload["events"]:
        event[field] = value


@pytest.mark.parametrize(
    ("field", "prefix"),
    [
        ("user_id", "usr_"),
        ("game_id", "game_"),
        ("chunk_id", "chk_"),
        ("event_id", "evt_"),
    ],
)
def test_v2_schema_and_model_share_identifier_suffix_limit(
    fixture_payload: dict[str, Any],
    field: str,
    prefix: str,
) -> None:
    _set_identifier_suffix_length(fixture_payload, field, prefix, 120)

    V2_VALIDATOR.validate(fixture_payload)
    ChunkEnvelope.model_validate(fixture_payload)

    _set_identifier_suffix_length(fixture_payload, field, prefix, 121)
    assert V2_VALIDATOR.is_valid(fixture_payload) is False
    with pytest.raises(ValidationError):
        ChunkEnvelope.model_validate(fixture_payload)


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("game_id", f"game_{'a' * 121}"),
        ("chunk_id", f"chk_{'a' * 121}"),
    ],
)
def test_ingest_acknowledgment_bounds_key_forming_identifiers(
    field: str,
    value: str,
) -> None:
    payload = {
        "accepted": True,
        "duplicate": False,
        "game_id": "game_01",
        "chunk_id": "chk_01",
        "chunk_sequence": 1,
        "accepted_event_range": {"first": 1, "last": 1},
        "queued": True,
        "server_received_at_ms": 1,
    }
    payload[field] = value

    with pytest.raises(ValidationError):
        IngestAcknowledgment.model_validate(payload)


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("schema_version", 1),
        ("user_id", "user_invalid"),
        ("game_id", "invalid"),
        ("game_type", ""),
        ("game_type", "tetris\nx-amz-meta-injected: 1"),
        ("game_type", "t" * 65),
        ("game_version", ""),
        ("chunk_id", "invalid"),
    ],
)
def test_v2_envelope_rejects_invalid_top_level_format(
    fixture_payload: dict[str, Any], field: str, value: object
) -> None:
    fixture_payload[field] = value

    with pytest.raises(ValidationError):
        ChunkEnvelope.model_validate(fixture_payload)


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("chunk_sequence", 0),
        ("first_event_sequence", 0),
        ("last_event_sequence", 0),
        ("event_count", 0),
        ("created_at_ms", -1),
    ],
)
def test_v2_envelope_rejects_out_of_range_numeric_fields(
    fixture_payload: dict[str, Any], field: str, value: int
) -> None:
    fixture_payload[field] = value

    with pytest.raises(ValidationError):
        ChunkEnvelope.model_validate(fixture_payload)


def test_v2_envelope_rejects_event_count_mismatch(fixture_payload: dict[str, Any]) -> None:
    fixture_payload["event_count"] += 1

    with pytest.raises(ValidationError, match="event_count must equal events length"):
        ChunkEnvelope.model_validate(fixture_payload)


def test_v2_envelope_rejects_first_event_range_mismatch(
    fixture_payload: dict[str, Any],
) -> None:
    fixture_payload["first_event_sequence"] += 1

    with pytest.raises(
        ValidationError,
        match="first_event_sequence must match the first event",
    ):
        ChunkEnvelope.model_validate(fixture_payload)


def test_v2_envelope_rejects_last_event_range_mismatch(
    fixture_payload: dict[str, Any],
) -> None:
    fixture_payload["last_event_sequence"] -= 1

    with pytest.raises(
        ValidationError,
        match="last_event_sequence must match the last event",
    ):
        ChunkEnvelope.model_validate(fixture_payload)


def test_v2_envelope_rejects_non_increasing_event_sequences(
    fixture_payload: dict[str, Any],
) -> None:
    fixture_payload["events"][2]["event_sequence"] = fixture_payload["events"][1][
        "event_sequence"
    ]

    with pytest.raises(ValidationError, match="event sequences must be strictly increasing"):
        ChunkEnvelope.model_validate(fixture_payload)


@pytest.mark.parametrize("field", ["game_id", "chunk_id"])
def test_v2_envelope_rejects_event_identity_that_does_not_match_chunk(
    fixture_payload: dict[str, Any], field: str
) -> None:
    fixture_payload["events"][2][field] = f"{field}_wrong"

    with pytest.raises(
        ValidationError,
        match=rf"events\[2\]\.{field} must match the chunk",
    ):
        ChunkEnvelope.model_validate(fixture_payload)


def test_v2_envelope_rejects_invalid_event_id_format(fixture_payload: dict[str, Any]) -> None:
    fixture_payload["events"][2]["event_id"] = "invalid"

    with pytest.raises(ValidationError, match=r"events\[2\]\.event_id has an invalid format"):
        ChunkEnvelope.model_validate(fixture_payload)


@pytest.mark.parametrize("value", [0, True, "3"])
def test_v2_envelope_rejects_invalid_event_sequence(
    fixture_payload: dict[str, Any], value: object
) -> None:
    fixture_payload["events"][2]["event_sequence"] = value

    with pytest.raises(
        ValidationError,
        match=r"events\[2\]\.event_sequence must be an integer >= 1",
    ):
        ChunkEnvelope.model_validate(fixture_payload)


def test_v2_envelope_rejects_authenticated_chunk_identity_mismatch(
    fixture_payload: dict[str, Any],
) -> None:
    envelope = ChunkEnvelope.model_validate(fixture_payload)

    with pytest.raises(ContractError, match="user_id must match"):
        envelope.validate_authenticated_identity("usr_someone_else")


def test_v2_envelope_rejects_authenticated_event_identity_mismatch(
    fixture_payload: dict[str, Any],
) -> None:
    fixture_payload["events"][2]["user_id"] = "usr_someone_else"
    envelope = ChunkEnvelope.model_validate(fixture_payload)

    with pytest.raises(
        ContractError,
        match=r"events\[2\]\.user_id must match the authenticated user",
    ):
        envelope.validate_authenticated_identity(fixture_payload["user_id"])


def _peer_repo() -> Path | None:
    """Locate a local tui-gamepigeon checkout, if one is available."""
    configured = os.environ.get("TUI_GAMEPIGEON_REPO")
    if configured:
        return Path(configured)
    sibling = ROOT.parent / "tui-gamepigeon"
    return sibling if sibling.is_dir() else None


@pytest.mark.skipif(
    _peer_repo() is None,
    reason="no local tui-gamepigeon checkout (set TUI_GAMEPIGEON_REPO to enable)",
)
def test_contracts_match_tui_gamepigeon_checkout() -> None:
    """Guard against issue #15: contract files must stay byte-identical across repos.

    Runs only when a local client checkout is discoverable, since CI here has
    no access to the sibling repo. See scripts/check_contract_drift.py.
    """
    import importlib.util

    spec = importlib.util.spec_from_file_location(
        "check_contract_drift", ROOT / "scripts/check_contract_drift.py"
    )
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)

    problems = module.find_drift(_peer_repo())  # type: ignore[arg-type]
    assert problems == []
