"""Small, allowlisted structured-event logger."""

import json


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
    if not isinstance(event, str):
        raise TypeError("event must be a string")

    fields = {
        "event": event,
        "request_id": request_id,
        "user_id": user_id,
        "game_id": game_id,
        "chunk_id": chunk_id,
        "outcome": outcome,
    }
    payload = {key: value for key, value in fields.items() if value is not None}
    print(json.dumps(payload, separators=(",", ":")))
