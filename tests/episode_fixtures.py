"""Builders for realistic ``episode.ready`` messages and validated chunk artifacts."""

import json
from datetime import UTC, datetime
from typing import Any

USER_ID = "usr_018f0000-0000-7000-8000-000000000000"
GAME_ID = "game_018f0000-0000-7000-8000-000000000001"
GAME_TYPE = "2048"
GAME_VERSION = "1"
FIRST_CLIENT_TIME_MS = 1_784_239_190_000
CHUNK_CREATED_AT_MS = int(datetime(2026, 7, 16, 9, tzinfo=UTC).timestamp() * 1000)
COMPLETED_AT_MS = int(datetime(2026, 7, 17, 12, tzinfo=UTC).timestamp() * 1000)
WRITTEN_AT_MS = COMPLETED_AT_MS + 123
CHUNK_DATE = "2026-07-16"
PARQUET_KEY = (
    f"episodes/parquet/v1/{GAME_TYPE}/2026/07/17/user={USER_ID}/{GAME_ID}.parquet"
)
MANIFEST_KEY = PARQUET_KEY.removesuffix(".parquet") + ".manifest.json"


def chunk_id(sequence: int) -> str:
    return f"chk_018f0000-0000-7000-8000-{sequence:012d}"


def _event(
    sequence: int,
    chunk: str,
    event_type: str,
    payload: dict[str, Any],
) -> dict[str, Any]:
    return {
        "schema_version": 2,
        "user_id": USER_ID,
        "game_id": GAME_ID,
        "chunk_id": chunk,
        "event_id": f"evt_018f0000-0000-7000-8000-{sequence:012d}",
        "event_sequence": sequence,
        "event_type": event_type,
        "event_version": 1,
        "visibility": "policy",
        "client_time_ms": FIRST_CLIENT_TIME_MS + sequence,
        "monotonic_time_us": sequence * 1_000,
        "simulation_tick": sequence,
        "payload": payload,
    }


def build_events(step_count: int, *, ended: bool) -> list[dict[str, Any]]:
    """``episode_started``, then observation/control/outcome per step."""
    events = [
        _event(
            1,
            chunk_id(1),
            "episode_started",
            {
                "game_type": GAME_TYPE,
                "game_version": GAME_VERSION,
                "ruleset_id": "classic",
            },
        )
    ]
    controls = ("left", "right", "up", "down")
    for step in range(step_count):
        base = 2 + step * 3
        control_event_id = f"evt_018f0000-0000-7000-8000-{base + 1:012d}"
        events.append(
            _event(
                base,
                chunk_id(1),
                "observation",
                {
                    "observation_id": f"obs_{step:04d}",
                    "representation": "engine_state_v1",
                    "state": {"board": [2 * (step + 1), 0, 0, 0]},
                },
            )
        )
        events.append(
            _event(
                base + 1,
                chunk_id(1),
                "control_changed",
                {
                    "based_on_observation_id": f"obs_{step:04d}",
                    "control": controls[step % len(controls)],
                    "action": controls[step % len(controls)],
                    "kind": "impulse",
                    "actor": "human",
                },
            )
        )
        events.append(
            _event(
                base + 2,
                chunk_id(1),
                "outcome_changed",
                {
                    "caused_by_event_id": control_event_id,
                    "score_before": step * 10,
                    "score_after": (step + 1) * 10,
                    "score_delta": 10,
                    "engine_reward": step * 2,
                    "terminal": False,
                    "progress": {"moved": True, "success": True},
                },
            )
        )
    if ended:
        events.append(
            _event(
                2 + step_count * 3,
                chunk_id(1),
                "episode_ended",
                {"reason": "game_over", "final_score": step_count * 10},
            )
        )
    return events


def split_into_chunks(
    events: list[dict[str, Any]],
    sizes: list[int],
) -> list[dict[str, Any]]:
    """Partition events into v2 chunk envelopes of the given event counts."""
    envelopes: list[dict[str, Any]] = []
    offset = 0
    for index, size in enumerate(sizes):
        sequence = index + 1
        window = [dict(event) for event in events[offset : offset + size]]
        offset += size
        for event in window:
            event["chunk_id"] = chunk_id(sequence)
        envelopes.append(
            {
                "schema_version": 2,
                "user_id": USER_ID,
                "game_id": GAME_ID,
                "game_type": GAME_TYPE,
                "game_version": GAME_VERSION,
                "chunk_id": chunk_id(sequence),
                "chunk_sequence": sequence,
                "first_event_sequence": window[0]["event_sequence"],
                "last_event_sequence": window[-1]["event_sequence"],
                "event_count": len(window),
                "created_at_ms": CHUNK_CREATED_AT_MS + index * 1_000,
                "client": {
                    "platform": "tui",
                    "application_version": "0.2.0",
                    "recording_sdk_version": "2",
                },
                "events": window,
            }
        )
    return envelopes


def validated_key(envelope: dict[str, Any]) -> str:
    return (
        f"validated/date={CHUNK_DATE}/user={USER_ID}"
        f"/game={GAME_ID}/chunk={envelope['chunk_id']}.json"
    )


def validated_artifact(envelope: dict[str, Any]) -> bytes:
    """The exact artifact shape ``GameplayConsumer`` writes."""
    return json.dumps(
        {
            "artifact_version": 1,
            "authenticated_user_id": USER_ID,
            "chunk": envelope,
            "payload_sha256": "0" * 64,
            "pointer_received_at_ms": envelope["created_at_ms"],
        },
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")


def seed_chunks(bucket: Any, envelopes: list[dict[str, Any]]) -> None:
    for envelope in envelopes:
        body = validated_artifact(envelope)
        bucket.seed_raw(validated_key(envelope), body, "0" * 64)


def episode_message(
    envelopes: list[dict[str, Any]],
    *,
    status: str = "complete",
    completed_at_ms: int = COMPLETED_AT_MS,
) -> dict[str, Any]:
    return {
        "version": 1,
        "status": status,
        "user_id": USER_ID,
        "game_id": GAME_ID,
        "game_type": GAME_TYPE,
        "game_version": GAME_VERSION,
        "chunk_count": len(envelopes),
        "first_event_sequence": envelopes[0]["first_event_sequence"],
        "last_event_sequence": envelopes[-1]["last_event_sequence"],
        "chunks": [
            {
                "chunk_id": envelope["chunk_id"],
                "chunk_sequence": envelope["chunk_sequence"],
                "created_at_ms": envelope["created_at_ms"],
            }
            for envelope in envelopes
        ],
        "completed_at_ms": completed_at_ms,
    }


def three_chunk_episode(
    *,
    status: str = "complete",
    ended: bool = True,
    step_count: int = 5,
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    """A 5-step episode split across three chunks, plus its ready message."""
    events = build_events(step_count, ended=ended)
    total = len(events)
    first = total // 3
    second = total // 3
    envelopes = split_into_chunks(events, [first, second, total - first - second])
    return envelopes, episode_message(envelopes, status=status)
