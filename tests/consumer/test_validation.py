import copy
import json
from dataclasses import replace
from pathlib import Path

import pytest

from retro_backend.consumer.types import BrokerRecord
from retro_backend.consumer.validation import RecordValidationError, decode_v2_record

FIXTURE = (
    Path(__file__).resolve().parents[2]
    / "contracts"
    / "fixtures"
    / "interaction-trace-chunk-v2.json"
)


def make_record(payload: dict[str, object], *, user_id: str | None = None) -> BrokerRecord:
    authenticated_user = user_id or str(payload["user_id"])
    return BrokerRecord(
        topic="retro-game-events",
        partition=3,
        offset=42,
        timestamp_ms=1_784_239_200_123,
        key=b"game-key",
        value=json.dumps(payload).encode(),
        headers=(("retro-user-id", authenticated_user.encode()),),
    )


@pytest.fixture
def payload() -> dict[str, object]:
    return json.loads(FIXTURE.read_text())


def test_decode_v2_record_flattens_every_event_with_authoritative_identity(
    payload: dict[str, object],
) -> None:
    rows = decode_v2_record(make_record(payload))

    assert len(rows) == 5
    assert rows[0].user_id == payload["user_id"]
    assert rows[0].game_type == "tetris"
    assert rows[0].broker_partition == 3
    assert rows[0].broker_offset == 42
    assert rows[0].ingested_at_ms == 1_784_239_200_123
    assert json.loads(rows[0].payload_json)["game_type"] == "tetris"
    assert [row.event_sequence for row in rows] == [1, 2, 3, 4, 5]


def test_payload_json_preserves_the_complete_original_parsed_payload(
    payload: dict[str, object],
) -> None:
    events = payload["events"]
    assert isinstance(events, list)
    observation = events[1]
    assert isinstance(observation, dict)
    original_payload = observation["payload"]
    assert isinstance(original_payload, dict)
    assert "capture_timing" not in original_payload
    assert "caused_by_event_id" not in original_payload

    rows = decode_v2_record(make_record(payload))

    expected = json.dumps(original_payload, sort_keys=True, separators=(",", ":"))
    assert rows[1].payload_json == expected
    decoded = json.loads(rows[1].payload_json)
    assert decoded == original_payload
    cells = decoded["cells"]
    assert isinstance(cells, list)
    cell = cells[0]
    assert isinstance(cell, dict)
    assert type(cell["foreground"]) is int
    assert cell["foreground"] == 15
    assert "capture_timing" not in decoded
    assert "caused_by_event_id" not in decoded


@pytest.mark.parametrize(
    ("case", "message"),
    [
        ("missing_client", "client"),
        ("wrong_schema", "schema_version"),
        ("extra_field", "extra"),
        ("wrong_count", "event_count"),
        ("sequence_gap", "event_sequence"),
        ("wrong_game", "game_id"),
        ("wrong_event_type", "event_type"),
    ],
)
def test_decode_v2_record_rejects_incomplete_or_inconsistent_chunks(
    payload: dict[str, object], case: str, message: str
) -> None:
    invalid = copy.deepcopy(payload)
    if case == "missing_client":
        invalid.pop("client")
    elif case == "wrong_schema":
        invalid["schema_version"] = 1
    elif case == "extra_field":
        invalid["extra"] = True
    elif case == "wrong_count":
        invalid["event_count"] = 99
    else:
        events = invalid["events"]
        assert isinstance(events, list)
        index = 1 if case == "sequence_gap" else 0
        event = events[index]
        assert isinstance(event, dict)
        if case == "sequence_gap":
            event["event_sequence"] = 8
        elif case == "wrong_game":
            event["game_id"] = "game_other"
        else:
            event["event_type"] = "unknown"

    with pytest.raises(RecordValidationError, match=message):
        decode_v2_record(make_record(invalid))


def test_decode_v2_record_rejects_embedded_user_mismatch(payload: dict[str, object]) -> None:
    with pytest.raises(RecordValidationError, match="authenticated user"):
        decode_v2_record(make_record(payload, user_id="usr_someone-else"))


def test_decode_v2_record_requires_one_utf8_user_header(payload: dict[str, object]) -> None:
    record = make_record(payload)
    missing = replace(record, headers=())
    duplicate = replace(
        record,
        headers=(
            ("retro-user-id", str(payload["user_id"]).encode()),
            ("retro-user-id", str(payload["user_id"]).encode()),
        ),
    )

    with pytest.raises(RecordValidationError, match="retro-user-id"):
        decode_v2_record(missing)
    with pytest.raises(RecordValidationError, match="retro-user-id"):
        decode_v2_record(duplicate)


@pytest.mark.parametrize(
    ("path", "value"),
    [
        (("chunk_sequence",), True),
        (("chunk_sequence",), 2**70),
        (("events", 0, "event_sequence"), True),
        (("events", 0, "monotonic_time_us"), True),
        (("events", 0, "monotonic_time_us"), 2**70),
        (("events", 0, "simulation_tick"), -1),
        (("events", 0, "simulation_tick"), 2**70),
        (("events", 0, "payload", "viewport", "width"), True),
        (("events", 0, "payload", "viewport", "height"), -1),
        (("events", 4, "payload", "score_before"), "0"),
        (("events", 4, "payload", "terminal"), 0),
    ],
)
def test_decode_v2_record_rejects_coercion_and_out_of_range_values(
    payload: dict[str, object], path: tuple[str | int, ...], value: object
) -> None:
    invalid = copy.deepcopy(payload)
    target: object = invalid
    for component in path[:-1]:
        if isinstance(target, dict):
            if not isinstance(component, str):
                raise AssertionError("invalid dictionary test mutation path")
            target = target[component]
            continue
        if isinstance(target, list):
            if not isinstance(component, int):
                raise AssertionError("invalid list test mutation path")
            target = target[component]
            continue
        raise AssertionError("invalid test mutation path")
    final = path[-1]
    if isinstance(target, dict):
        if not isinstance(final, str):
            raise AssertionError("invalid dictionary test mutation target")
        target[final] = value
    elif isinstance(target, list):
        if not isinstance(final, int):
            raise AssertionError("invalid list test mutation target")
        target[final] = value
    else:
        raise AssertionError("invalid test mutation target")

    with pytest.raises(RecordValidationError):
        decode_v2_record(make_record(invalid))


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("partition", -1),
        ("partition", 2**40),
        ("offset", True),
        ("offset", 2**70),
        ("timestamp_ms", -1),
        ("timestamp_ms", 2**70),
    ],
)
def test_decode_v2_record_rejects_out_of_range_broker_coordinates(
    payload: dict[str, object], field: str, value: object
) -> None:
    record = replace(make_record(payload), **{field: value})

    with pytest.raises(RecordValidationError, match=field):
        decode_v2_record(record)


def _with_episode_ended(payload: dict[str, object]) -> dict[str, object]:
    result = copy.deepcopy(payload)
    events = result["events"]
    assert isinstance(events, list)
    previous = events[-1]
    assert isinstance(previous, dict)
    event = copy.deepcopy(previous)
    event.update(
        {
            "event_id": "evt_018f0000-0000-7000-8000-000000000008",
            "event_sequence": 6,
            "event_type": "episode_ended",
            "monotonic_time_us": 103000,
            "payload": {
                "reason": "game_over",
                "final_event_sequence": 6,
                "final_score": 123,
                "duration_us": 103000,
                "completed": True,
            },
        }
    )
    events.append(event)
    result["last_event_sequence"] = 6
    result["event_count"] = 6
    return result


def test_decode_v2_record_accepts_complete_episode_ended_payload(
    payload: dict[str, object],
) -> None:
    rows = decode_v2_record(make_record(_with_episode_ended(payload)))

    assert rows[-1].event_type == "episode_ended"
    assert json.loads(rows[-1].payload_json)["completed"] is True


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("completed", 1),
        ("duration_us", 2**70),
        ("final_event_sequence", 5),
        ("reason", "crashed"),
    ],
)
def test_decode_v2_record_rejects_malformed_episode_ended_payload(
    payload: dict[str, object], field: str, value: object
) -> None:
    invalid = _with_episode_ended(payload)
    events = invalid["events"]
    assert isinstance(events, list)
    event = events[-1]
    assert isinstance(event, dict)
    event_payload = event["payload"]
    assert isinstance(event_payload, dict)
    event_payload[field] = value

    with pytest.raises(RecordValidationError):
        decode_v2_record(make_record(invalid))
