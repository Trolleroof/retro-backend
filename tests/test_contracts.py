import copy
import json
from pathlib import Path

import pytest

from retro_backend.contracts import ValidationError, validate_chunk_envelope

ROOT = Path(__file__).resolve().parents[1]
V1_FIXTURE = ROOT / "contracts/fixtures/game-event-chunk-v1.json"
V2_FIXTURE = ROOT / "contracts/fixtures/interaction-trace-chunk-v2.json"


def test_accepts_v2_client_envelope() -> None:
    payload = json.loads(V2_FIXTURE.read_bytes())
    validated = validate_chunk_envelope(payload)
    assert validated["schema_version"] == payload["schema_version"]
    assert validated["chunk_id"] == payload["chunk_id"]


def test_rejects_v1_client_envelope() -> None:
    payload = json.loads(V1_FIXTURE.read_bytes())

    with pytest.raises(ValidationError, match="schema_version must be 2"):
        validate_chunk_envelope(payload)


def test_envelope_validation_does_not_inspect_event_semantics() -> None:
    payload = json.loads(V2_FIXTURE.read_bytes())
    payload["events"][0] = {"unvalidated": "consumer-owned"}
    assert validate_chunk_envelope(payload) is payload


def test_envelope_validation_rejects_missing_routing_fields() -> None:
    payload = json.loads(V2_FIXTURE.read_bytes())
    payload.pop("game_id")
    with pytest.raises(ValidationError, match="missing required fields: game_id"):
        validate_chunk_envelope(payload)


def test_envelope_validation_does_not_mutate_request() -> None:
    payload = json.loads(V2_FIXTURE.read_bytes())
    original = copy.deepcopy(payload)
    validate_chunk_envelope(payload)
    assert payload == original
