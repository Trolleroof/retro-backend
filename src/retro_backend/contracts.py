"""Validation for the versioned arcade-to-backend wire contract."""

from __future__ import annotations

import re
from typing import Any

GAME_ID = re.compile(r"^game_[A-Za-z0-9._-]+$")
CHUNK_ID = re.compile(r"^chk_[A-Za-z0-9._-]+$")
USER_ID = re.compile(r"^usr_[A-Za-z0-9._-]+$")
EVENT_ID = re.compile(r"^evt_[A-Za-z0-9._-]+$")
TRACE_EVENT_TYPES = {
    "episode_started",
    "observation",
    "control_changed",
    "control_checkpoint",
    "outcome_changed",
    "engine_checkpoint",
    "episode_ended",
}


class ValidationError(ValueError):
    """Raised when an ingestion payload violates the shared contract."""


def _object(value: Any, name: str) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise ValidationError(f"{name} must be an object")
    return value


def _string(value: Any, name: str) -> str:
    if not isinstance(value, str) or not value:
        raise ValidationError(f"{name} must be a non-empty string")
    return value


def _integer(value: Any, name: str, minimum: int = 1) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < minimum:
        raise ValidationError(f"{name} must be an integer >= {minimum}")
    return value


def _validate_v1_event(event: dict[str, Any], index: int) -> None:
    for field in ("source", "monotonic_time_ms"):
        if field not in event:
            raise ValidationError(f"events[{index}] is missing {field}")
    if event["source"] not in {"player", "environment", "system"}:
        raise ValidationError(f"events[{index}].source is invalid")


def _validate_v2_event(
    event: dict[str, Any],
    index: int,
    *,
    user_id: str,
    game_id: str,
    chunk_id: str,
) -> None:
    for field in (
        "schema_version",
        "user_id",
        "game_id",
        "chunk_id",
        "visibility",
        "monotonic_time_us",
    ):
        if field not in event:
            raise ValidationError(f"events[{index}] is missing {field}")
    if event["schema_version"] != 2:
        raise ValidationError(f"events[{index}].schema_version must be 2")
    for field, expected in (
        ("user_id", user_id),
        ("game_id", game_id),
        ("chunk_id", chunk_id),
    ):
        if event[field] != expected:
            raise ValidationError(f"events[{index}].{field} must match the chunk")
    if event["visibility"] not in {"policy", "privileged"}:
        raise ValidationError(f"events[{index}].visibility is invalid")
    if event["event_type"] not in TRACE_EVENT_TYPES:
        raise ValidationError(f"events[{index}].event_type is invalid")
    if event["event_type"] == "engine_checkpoint":
        if event["visibility"] != "privileged":
            raise ValidationError("engine_checkpoint visibility must be privileged")
    elif event["visibility"] != "policy":
        raise ValidationError(f"events[{index}] public event visibility must be policy")
    monotonic = event["monotonic_time_us"]
    if isinstance(monotonic, bool) or not isinstance(monotonic, int) or monotonic < 0:
        raise ValidationError(f"events[{index}].monotonic_time_us must be an integer >= 0")


def validate_chunk(value: Any) -> dict[str, Any]:
    """Validate a v1 or v2 chunk and return the original mapping."""
    chunk = _object(value, "chunk")
    required = {
        "schema_version",
        "game_id",
        "game_type",
        "game_version",
        "chunk_id",
        "chunk_sequence",
        "first_event_sequence",
        "last_event_sequence",
        "event_count",
        "created_at_ms",
        "client",
        "events",
    }
    missing = sorted(required.difference(chunk))
    if missing:
        raise ValidationError(f"missing required fields: {', '.join(missing)}")
    schema_version = chunk["schema_version"]
    if schema_version not in {1, 2}:
        raise ValidationError("schema_version must be 1 or 2")

    game_id = _string(chunk["game_id"], "game_id")
    chunk_id = _string(chunk["chunk_id"], "chunk_id")
    if not GAME_ID.fullmatch(game_id):
        raise ValidationError("game_id has an invalid format")
    if not CHUNK_ID.fullmatch(chunk_id):
        raise ValidationError("chunk_id has an invalid format")
    user_id = ""
    if schema_version == 2:
        user_id = _string(chunk.get("user_id"), "user_id")
        if not USER_ID.fullmatch(user_id):
            raise ValidationError("user_id has an invalid format")
    game_type = _string(chunk["game_type"], "game_type")
    game_version = _string(chunk["game_version"], "game_version")
    _integer(chunk["chunk_sequence"], "chunk_sequence")
    first = _integer(chunk["first_event_sequence"], "first_event_sequence")
    last = _integer(chunk["last_event_sequence"], "last_event_sequence")
    event_count = _integer(chunk["event_count"], "event_count")

    client = _object(chunk["client"], "client")
    for field in ("platform", "application_version", "recording_sdk_version"):
        _string(client.get(field), f"client.{field}")

    events = chunk["events"]
    if not isinstance(events, list) or not events:
        raise ValidationError("events must be a non-empty array")
    if event_count != len(events):
        raise ValidationError("event_count must equal events length")

    sequences: list[int] = []
    for index, event_value in enumerate(events):
        event = _object(event_value, f"events[{index}]")
        for field in (
            "event_id",
            "event_sequence",
            "event_type",
            "event_version",
            "client_time_ms",
            "payload",
        ):
            if field not in event:
                raise ValidationError(f"events[{index}] is missing {field}")
        event_id = _string(event["event_id"], f"events[{index}].event_id")
        if not EVENT_ID.fullmatch(event_id):
            raise ValidationError(f"events[{index}].event_id has an invalid format")
        sequences.append(_integer(event["event_sequence"], f"events[{index}].event_sequence"))
        if event["event_version"] != 1:
            raise ValidationError(f"events[{index}].event_version must be 1")
        _string(event["event_type"], f"events[{index}].event_type")
        _object(event["payload"], f"events[{index}].payload")
        if schema_version == 1:
            _validate_v1_event(event, index)
        else:
            _validate_v2_event(
                event,
                index,
                user_id=user_id,
                game_id=game_id,
                chunk_id=chunk_id,
            )

    if any(
        current <= previous for previous, current in zip(sequences, sequences[1:], strict=False)
    ):
        raise ValidationError("event sequences must be strictly increasing")
    if sequences[0] != first:
        raise ValidationError("first_event_sequence must match the first event")
    if sequences[-1] != last:
        raise ValidationError("last_event_sequence must match the last event")

    start_type = "game_started" if schema_version == 1 else "episode_started"
    for start in (event for event in events if event["event_type"] == start_type):
        payload = _object(start["payload"], f"{start_type}.payload")
        if payload.get("game_type") != game_type:
            raise ValidationError(f"{start_type} game_type must match the chunk")
        if payload.get("game_version") != game_version:
            raise ValidationError(f"{start_type} game_version must match the chunk")
    return chunk
