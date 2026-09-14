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

# ``bc-export.ts`` orders events with ``String.prototype.localeCompare``, which
# is ICU collation, not code-point order. Over the alphabet the contract allows
# for game ids (``^game_[A-Za-z0-9._-]+$``) that ordering is: ``_`` < ``-`` <
# ``.`` < digits < letters, where letters compare case-insensitively first and
# only break ties by case (lowercase before uppercase). Python's native ``str``
# comparison is code-point order and disagrees: ICU puts ``game__`` before
# ``game_9`` and ``game_a`` before ``game_A``, code point does the reverse of
# both. So sorting on the raw string reorders samples and,
# when an observation_id is reused across games, changes which sample is emitted
# at all (the observation/outcome/ruleset lookups are last-write-wins over this
# ordering). Each entry below is one primary-weight class, ordered by primary
# weight; position within a class is the case (tertiary) weight.
_COLLATION_CLASSES: tuple[str, ...] = (
    "_",
    "-",
    ".",
    *"0123456789",
    *(f"{letter}{letter.upper()}" for letter in "abcdefghijklmnopqrstuvwxyz"),
)

_COLLATION_WEIGHTS: dict[str, tuple[int, int]] = {
    char: (primary, tertiary)
    for primary, klass in enumerate(_COLLATION_CLASSES)
    for tertiary, char in enumerate(klass)
}

# Characters outside the contract's game-id alphabet have no ICU weight here;
# they sort after every known character in code-point order so the result stays
# deterministic. Only the contract alphabet is guaranteed to match localeCompare.
_UNKNOWN_PRIMARY_BASE = len(_COLLATION_CLASSES)


def _game_id_sort_key(game_id: str) -> tuple[tuple[int, ...], tuple[int, ...]]:
    """Collation key reproducing ``localeCompare`` for contract game ids.

    Multi-level, like ICU: every character's primary weight is compared across
    the whole string before any case difference is considered.
    """
    weights = [
        _COLLATION_WEIGHTS.get(char, (_UNKNOWN_PRIMARY_BASE + ord(char), 0)) for char in game_id
    ]
    return tuple(primary for primary, _ in weights), tuple(tertiary for _, tertiary in weights)


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
        key=lambda event: (
            _game_id_sort_key(event["game_id"]),
            event["event_sequence"],
        ),
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

    Absent payload fields read as ``None`` rather than raising, matching how the
    TypeScript reads a missing property; the sole divergence is that a control
    whose ``action`` is not a string is skipped instead of emitting a sample no
    Parquet row can hold. Neither changes the output for a conformant trace.
    """
    actor_set = set(actors) if actors is not None else set(DEFAULT_ACTORS)
    events = _ordered_unique_events(chunks)
    observations: dict[str, dict[str, Any]] = {}
    outcomes: dict[str, dict[str, Any]] = {}
    rulesets: dict[str, str | None] = {}
    chunks_by_game = {chunk["game_id"]: chunk for chunk in chunks}

    # Every payload field below is read with ``.get``. The v2 event contract
    # guarantees ``payload`` is an object but says nothing about what is inside
    # it, so a contract-valid trace may omit any of these. In TypeScript that
    # reads as ``undefined`` and flows through; subscripting here would raise
    # ``KeyError``, which ``build_rows`` turns into an ``EpisodeConversionError``
    # that discards the *whole episode* over one absent field. ``ruleset_id`` is
    # the live case: nothing in the contract or the published JSON Schema
    # requires it, and the Parquet column is already OPTIONAL.
    for event in events:
        event_type = event.get("event_type")
        payload = event.get("payload")
        if not isinstance(payload, Mapping):
            continue
        if event_type == "observation":
            # An id that is not a string can never be matched by a
            # ``based_on_observation_id`` lookup below, which is guarded against
            # falsy values, so declining to index it is what TypeScript does.
            observation_id = payload.get("observation_id")
            if isinstance(observation_id, str):
                observations[observation_id] = event
        elif event_type == "outcome_changed":
            caused_by_event_id = payload.get("caused_by_event_id")
            if isinstance(caused_by_event_id, str):
                outcomes[caused_by_event_id] = event
        elif event_type == "episode_started":
            # Stored even when absent, so a later episode_started still wins the
            # last-write-wins race the ordering above establishes.
            ruleset_id = payload.get("ruleset_id")
            rulesets[event["game_id"]] = (
                ruleset_id if isinstance(ruleset_id, str) else None
            )

    samples: list[dict[str, Any]] = []
    for event in events:
        if event.get("event_type") != "control_changed":
            continue
        payload = event["payload"]
        actor = payload.get("actor")
        based_on_observation_id = payload.get("based_on_observation_id")
        if actor not in actor_set or not based_on_observation_id:
            continue
        # The one deliberate divergence from bc-export.ts: TypeScript emits a
        # sample whose ``action`` is ``undefined``, which this pipeline cannot
        # serialize -- ``sample_to_row`` rejects it and the episode is lost.
        # Dropping the one malformed control is strictly better, and no
        # contract-valid control is affected.
        action = payload.get("action")
        if not isinstance(action, str):
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
                "client_time_ms": event["client_time_ms"],
                "control_event_id": event["event_id"],
                "observation_id": observation["payload"]["observation_id"],
                "observation": _policy_observation(observation["payload"]),
                "action": action,
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
