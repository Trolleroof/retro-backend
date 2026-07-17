import copy
import json
from pathlib import Path

import pytest

from retro_backend.contracts import ValidationError, validate_chunk

ROOT = Path(__file__).resolve().parents[1]
V1_FIXTURE = ROOT / "contracts/fixtures/game-event-chunk-v1.json"
V2_FIXTURE = ROOT / "contracts/fixtures/interaction-trace-chunk-v2.json"


@pytest.mark.parametrize("fixture", [V1_FIXTURE, V2_FIXTURE])
def test_accepts_versioned_client_contracts(fixture: Path) -> None:
    payload = json.loads(fixture.read_bytes())
    validated = validate_chunk(payload)
    assert validated["schema_version"] == payload["schema_version"]
    assert validated["event_count"] == len(payload["events"])


def test_rejects_v2_event_identity_that_does_not_match_chunk() -> None:
    payload = json.loads(V2_FIXTURE.read_bytes())
    payload["events"][2]["chunk_id"] = "chk_wrong"
    with pytest.raises(ValidationError, match="chunk_id must match"):
        validate_chunk(payload)


def test_rejects_non_increasing_event_sequences() -> None:
    payload = json.loads(V2_FIXTURE.read_bytes())
    payload["events"][2]["event_sequence"] = payload["events"][1]["event_sequence"]
    with pytest.raises(ValidationError, match="strictly increasing"):
        validate_chunk(payload)


def test_rejects_privileged_policy_event() -> None:
    payload = json.loads(V2_FIXTURE.read_bytes())
    observation = next(event for event in payload["events"] if event["event_type"] == "observation")
    observation["visibility"] = "privileged"
    with pytest.raises(ValidationError, match="public event visibility"):
        validate_chunk(payload)


def test_validation_does_not_mutate_request() -> None:
    payload = json.loads(V2_FIXTURE.read_bytes())
    original = copy.deepcopy(payload)
    validate_chunk(payload)
    assert payload == original
