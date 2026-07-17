"""Lightweight validation for versioned chunk envelopes."""

from __future__ import annotations

import re
from typing import Any

GAME_ID = re.compile(r"^game_[A-Za-z0-9._-]+$")
CHUNK_ID = re.compile(r"^chk_[A-Za-z0-9._-]+$")
USER_ID = re.compile(r"^usr_[A-Za-z0-9._-]+$")


class ValidationError(ValueError):
    """Raised when a producer payload is not a supported chunk envelope."""


def _string(value: Any, name: str) -> str:
    if not isinstance(value, str) or not value:
        raise ValidationError(f"{name} must be a non-empty string")
    return value


def _integer(value: Any, name: str, minimum: int = 1) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < minimum:
        raise ValidationError(f"{name} must be an integer >= {minimum}")
    return value


def validate_chunk_envelope(value: Any) -> dict[str, Any]:
    """Validate routing and acknowledgment fields without inspecting event semantics."""
    if not isinstance(value, dict):
        raise ValidationError("chunk must be an object")
    chunk: dict[str, Any] = value
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
    if isinstance(schema_version, bool) or schema_version != 2:
        raise ValidationError("schema_version must be 2")

    game_id = _string(chunk["game_id"], "game_id")
    chunk_id = _string(chunk["chunk_id"], "chunk_id")
    if not GAME_ID.fullmatch(game_id):
        raise ValidationError("game_id has an invalid format")
    if not CHUNK_ID.fullmatch(chunk_id):
        raise ValidationError("chunk_id has an invalid format")
    user_id = _string(chunk.get("user_id"), "user_id")
    if not USER_ID.fullmatch(user_id):
        raise ValidationError("user_id has an invalid format")

    _string(chunk["game_type"], "game_type")
    _string(chunk["game_version"], "game_version")
    _integer(chunk["chunk_sequence"], "chunk_sequence")
    first = _integer(chunk["first_event_sequence"], "first_event_sequence")
    last = _integer(chunk["last_event_sequence"], "last_event_sequence")
    event_count = _integer(chunk["event_count"], "event_count")
    if last < first:
        raise ValidationError("last_event_sequence must not precede first_event_sequence")
    if not isinstance(chunk["client"], dict):
        raise ValidationError("client must be an object")
    events = chunk["events"]
    if not isinstance(events, list) or not events:
        raise ValidationError("events must be a non-empty array")
    if event_count != len(events):
        raise ValidationError("event_count must equal events length")
    return chunk


def validate_authenticated_identity(chunk: dict[str, Any], authenticated_user_id: str) -> None:
    """Reject v2 identity claims that disagree with the authenticated producer."""
    if chunk["user_id"] != authenticated_user_id:
        raise ValidationError("user_id must match the authenticated user")
    for index, event in enumerate(chunk["events"]):
        if (
            isinstance(event, dict)
            and "user_id" in event
            and event["user_id"] != authenticated_user_id
        ):
            raise ValidationError(f"events[{index}].user_id must match the authenticated user")
