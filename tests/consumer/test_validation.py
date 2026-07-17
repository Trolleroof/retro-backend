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
