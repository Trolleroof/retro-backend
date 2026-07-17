"""Small, allowlisted structured-event logger."""

import json
import re

ALLOWED_EVENTS = frozenset({"score_submitted"})
ALLOWED_OUTCOMES = frozenset({"accepted"})

REQUEST_ID_PATTERN = re.compile(r"^req_[A-Za-z0-9._-]{1,120}$")
USER_ID_PATTERN = re.compile(r"^usr_[A-Za-z0-9._-]{1,120}$")
GAME_ID_PATTERN = re.compile(r"^game_[A-Za-z0-9._-]{1,120}$")
CHUNK_ID_PATTERN = re.compile(r"^chk_[A-Za-z0-9._-]{1,120}$")

_IDENTIFIER_PATTERNS = {
    "request_id": REQUEST_ID_PATTERN,
    "user_id": USER_ID_PATTERN,
    "game_id": GAME_ID_PATTERN,
    "chunk_id": CHUNK_ID_PATTERN,
}


def _validate_choice(value: object, *, name: str, allowed: frozenset[str]) -> str:
    if not isinstance(value, str):
        raise TypeError(f"{name} must be a string")
    if value not in allowed:
        raise ValueError(f"{name} is not allowlisted")
    return value


def _validate_identifier(value: object, *, name: str) -> str:
    if not isinstance(value, str):
        raise TypeError(f"{name} must be a string")
    if _IDENTIFIER_PATTERNS[name].fullmatch(value) is None:
        raise ValueError(f"{name} has an invalid format")
    return value


def log_event(
    event: object,
    *,
    request_id: str | None = None,
    user_id: str | None = None,
    game_id: str | None = None,
    chunk_id: str | None = None,
    outcome: str | None = None,
) -> None:
    """Print one compact JSON object without accepting arbitrary context."""
    validated_event = _validate_choice(event, name="event", allowed=ALLOWED_EVENTS)

    fields = {
        "event": validated_event,
        "request_id": request_id,
        "user_id": user_id,
        "game_id": game_id,
        "chunk_id": chunk_id,
        "outcome": outcome,
    }
    for name, value in fields.items():
        if value is not None and name in _IDENTIFIER_PATTERNS:
            fields[name] = _validate_identifier(value, name=name)
    if outcome is not None:
        fields["outcome"] = _validate_choice(
            outcome,
            name="outcome",
            allowed=ALLOWED_OUTCOMES,
        )
    payload = {key: value for key, value in fields.items() if value is not None}
    print(json.dumps(payload, separators=(",", ":")))
