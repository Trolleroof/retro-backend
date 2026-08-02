"""Behavior-cloning sample extraction, ported from tui-gamepigeon's
``@tui-games/trace-export`` package (``packages/trace-export/src/bc-export.ts``).

Chosen approach for retro-backend#33: copy, not import — the Worker runs
Python (Pyodide), so the npm package isn't consumable at runtime. This module
mirrors ``extractBehaviorCloningSamples`` and ``splitForGame`` field-for-field
and is checked against the same fixtures via a golden test
(tests/test_bc_export.py) to avoid train/export skew.

Chunks and events are plain ``dict`` objects matching the v2 chunk envelope
contract (see ``retro_backend.contracts.ChunkEnvelope``), not typed models,
since the source TypeScript operates on the same loosely-typed JSON shape.
"""

from collections.abc import Iterable, Mapping, Sequence
from typing import Any, Literal

DEFAULT_ACTORS = ("human",)

DatasetSplit = Literal["train", "validation", "test"]


def _ordered_unique_events(
    chunks: Sequence[Mapping[str, Any]],
) -> list[dict[str, Any]]:
    events: dict[str, dict[str, Any]] = {}
    for chunk in chunks:
        if chunk.get("schema_version") != 2:
            continue
        for event in chunk.get("events", []):
            events[event["event_id"]] = event
    return sorted(
        events.values(),
        key=lambda event: (event["game_id"], event["event_sequence"]),
    )


def _policy_observation(payload: Mapping[str, Any]) -> dict[str, Any]:
    return {
        key: value
        for key, value in payload.items()
        if key not in ("observation_id", "caused_by_event_id")
    }


def extract_behavior_cloning_samples(
    chunks: Sequence[Mapping[str, Any]],
    *,
    actors: Iterable[str] | None = None,
    include_noops: bool = False,
) -> list[dict[str, Any]]:
    """Join observation -> control -> outcome events into BC samples.

    Mirrors ``extractBehaviorCloningSamples`` in bc-export.ts, including its
    cross-chunk dedup-by-event_id and its ``moved is False`` no-op filter.
    """
    actor_set = set(actors) if actors is not None else set(DEFAULT_ACTORS)
    events = _ordered_unique_events(chunks)
    observations: dict[str, dict[str, Any]] = {}
    outcomes: dict[str, dict[str, Any]] = {}
    rulesets: dict[str, str] = {}
    chunks_by_game = {chunk["game_id"]: chunk for chunk in chunks}

    for event in events:
        event_type = event.get("event_type")
        if event_type == "observation":
            observations[event["payload"]["observation_id"]] = event
        elif event_type == "outcome_changed":
            outcomes[event["payload"]["caused_by_event_id"]] = event
        elif event_type == "episode_started":
            rulesets[event["game_id"]] = event["payload"]["ruleset_id"]

    samples: list[dict[str, Any]] = []
    for event in events:
        if event.get("event_type") != "control_changed":
            continue
        payload = event["payload"]
        actor = payload.get("actor")
        based_on_observation_id = payload.get("based_on_observation_id")
        if actor not in actor_set or not based_on_observation_id:
            continue
        observation = observations.get(based_on_observation_id)
        if observation is None or observation["game_id"] != event["game_id"]:
            continue
        outcome = outcomes.get(event["event_id"])
        moved: bool | None = None
        if outcome is not None:
            progress = outcome["payload"].get("progress")
            if isinstance(progress, Mapping):
                moved_value = progress.get("moved")
                if isinstance(moved_value, bool):
                    moved = moved_value
        if not include_noops and moved is False:
            continue
        chunk = chunks_by_game.get(event["game_id"])
        if chunk is None:
            continue

        outcome_payload = outcome["payload"] if outcome is not None else None
        samples.append(
            {
                "schema_version": 1,
                "user_id": event["user_id"],
                "game_id": event["game_id"],
                "game_type": chunk["game_type"],
                "game_version": chunk["game_version"],
                "ruleset_id": rulesets.get(event["game_id"]),
                "event_sequence": event["event_sequence"],
                "control_event_id": event["event_id"],
                "observation_id": observation["payload"]["observation_id"],
                "observation": _policy_observation(observation["payload"]),
                "action": payload["action"],
                "actor": actor,
                "reward": outcome_payload.get("engine_reward")
                if outcome_payload is not None
                else None,
                "score_delta": outcome_payload.get("score_delta")
                if outcome_payload is not None
                else None,
                "terminal": outcome_payload.get("terminal")
                if outcome_payload is not None
                else None,
                "moved": moved,
            }
        )
    return samples


def split_for_game(game_id: str) -> DatasetSplit:
    """FNV-1a hash bucketing, ported bit-for-bit from ``splitForGame``."""
    hash_ = 2_166_136_261
    for byte in game_id.encode("utf-8"):
        hash_ ^= byte
        hash_ = (hash_ * 16_777_619) & 0xFFFFFFFF
    bucket = hash_ % 100
    if bucket < 90:
        return "train"
    if bucket < 95:
        return "validation"
    return "test"
