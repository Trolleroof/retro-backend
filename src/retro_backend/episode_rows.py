"""Project behavior-cloning samples onto the ``parquet/v1`` column schema.

The observation -> control -> outcome join is **not** reimplemented here. It
lives in :mod:`retro_backend.services.bc_export`, which is a field-for-field port
of ``extractBehaviorCloningSamples()`` from tui-gamepigeon's
``packages/trace-export/src/bc-export.ts`` and is pinned to that implementation's
real output by ``tests/fixtures/bc_export_golden.json``. This module only reads
the validated chunk artifacts out of R2, hands them to that function, and flattens
each returned sample into one Parquet row — so the row count is the golden
fixture's row count by construction.

An ``incomplete`` or ``abandoned`` episode is expected to have gaps: a validated
artifact that is missing (expired by the R2 lifecycle, or never written because
the chunk never arrived) is skipped rather than raised on, and the join simply
emits fewer samples.
"""

import json
import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

from .contracts import GAME_TYPE_PATTERN, EpisodeReady
from .episode_assembler import episode_object_name
from .parquet import ParquetColumn
from .services.bc_export import extract_behavior_cloning_samples
from .storage.r2 import R2Store, require_r2_key

PARQUET_SCHEMA_VERSION = 1

# Column order is normative; see docs/parquet-v1.md. `observation` is a nested
# object in the BC sample, so it travels as canonical JSON text. reward,
# score_delta, terminal and moved are genuinely absent when a control has no
# matching outcome_changed, so they are OPTIONAL rather than zero-filled.
EPISODE_COLUMNS: tuple[ParquetColumn, ...] = (
    ParquetColumn("schema_version", "INT32"),
    ParquetColumn("user_id", "UTF8"),
    ParquetColumn("game_id", "UTF8"),
    ParquetColumn("game_type", "UTF8"),
    ParquetColumn("game_version", "UTF8"),
    ParquetColumn("ruleset_id", "UTF8", optional=True),
    ParquetColumn("event_sequence", "INT64"),
    ParquetColumn("client_time_ms", "INT64"),
    ParquetColumn("control_event_id", "UTF8"),
    ParquetColumn("observation_id", "UTF8"),
    ParquetColumn("observation_json", "UTF8"),
    ParquetColumn("action", "UTF8"),
    ParquetColumn("actor", "UTF8"),
    ParquetColumn("reward", "DOUBLE", optional=True),
    ParquetColumn("score_delta", "INT64", optional=True),
    ParquetColumn("terminal", "BOOLEAN", optional=True),
    ParquetColumn("moved", "BOOLEAN", optional=True),
)

_GAME_TYPE_RE = re.compile(GAME_TYPE_PATTERN)


class EpisodeConversionError(ValueError):
    """A validated artifact did not satisfy the shape the BC join requires."""


def canonical_json(value: object) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False)


def episode_id_for(user_id: str, game_id: str) -> str:
    """``sha256(f"{user_id}:{game_id}")`` hex — the assembler's object name."""
    return episode_object_name(user_id, game_id)


def require_game_type(game_type: str) -> str:
    """Reject a game_type that cannot travel safely in an R2 key path."""
    if _GAME_TYPE_RE.fullmatch(game_type) is None:
        raise ValueError("game_type has an invalid format")
    return game_type


def utc_date_parts(epoch_ms: int) -> tuple[str, str, str]:
    moment = datetime.fromtimestamp(epoch_ms / 1000, UTC)
    return f"{moment.year:04d}", f"{moment.month:02d}", f"{moment.day:02d}"


def validated_chunk_key(
    *,
    user_id: str,
    game_id: str,
    chunk_id: str,
    created_at_ms: int,
) -> str:
    """The key ``GameplayConsumer`` wrote this chunk's validated artifact to."""
    partition_date = datetime.fromtimestamp(created_at_ms / 1000, UTC).date().isoformat()
    identity = f"user={user_id}/game={game_id}/chunk={chunk_id}"
    return require_r2_key(f"validated/date={partition_date}/{identity}.json")


def _text(sample: Mapping[str, Any], field: str) -> str:
    value = sample.get(field)
    if not isinstance(value, str):
        raise EpisodeConversionError(f"BC sample field {field} must be text")
    return value


def _optional_text(sample: Mapping[str, Any], field: str) -> str | None:
    value = sample.get(field)
    if value is None:
        return None
    if not isinstance(value, str):
        raise EpisodeConversionError(f"BC sample field {field} must be text or null")
    return value


def _integer(sample: Mapping[str, Any], field: str) -> int:
    value = sample.get(field)
    if isinstance(value, bool) or not isinstance(value, int):
        raise EpisodeConversionError(f"BC sample field {field} must be an integer")
    return value


def _optional_integer(sample: Mapping[str, Any], field: str) -> int | None:
    value = sample.get(field)
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, int):
        # A float score_delta is out of contract; truncating would be a silent lie.
        raise EpisodeConversionError(f"BC sample field {field} must be an integer or null")
    return value


def _optional_double(sample: Mapping[str, Any], field: str) -> float | None:
    value = sample.get(field)
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise EpisodeConversionError(f"BC sample field {field} must be numeric or null")
    return float(value)


def _optional_bool(sample: Mapping[str, Any], field: str) -> bool | None:
    value = sample.get(field)
    if value is None:
        return None
    if not isinstance(value, bool):
        raise EpisodeConversionError(f"BC sample field {field} must be a bool or null")
    return value


def sample_to_row(sample: Mapping[str, Any]) -> tuple[object, ...]:
    """Flatten one ``extract_behavior_cloning_samples`` result into a Parquet row."""
    try:
        observation_json = canonical_json(sample.get("observation"))
    except (TypeError, ValueError) as error:
        raise EpisodeConversionError("BC sample observation is not JSON-serializable") from error
    return (
        _integer(sample, "schema_version"),
        _text(sample, "user_id"),
        _text(sample, "game_id"),
        _text(sample, "game_type"),
        _text(sample, "game_version"),
        _optional_text(sample, "ruleset_id"),
        _integer(sample, "event_sequence"),
        _integer(sample, "client_time_ms"),
        _text(sample, "control_event_id"),
        _text(sample, "observation_id"),
        observation_json,
        _text(sample, "action"),
        _text(sample, "actor"),
        _optional_double(sample, "reward"),
        _optional_integer(sample, "score_delta"),
        _optional_bool(sample, "terminal"),
        _optional_bool(sample, "moved"),
    )


def samples_to_rows(samples: Sequence[Mapping[str, Any]]) -> list[tuple[object, ...]]:
    return [sample_to_row(sample) for sample in samples]


@dataclass(frozen=True, slots=True)
class EpisodeRowSet:
    rows: tuple[tuple[object, ...], ...]
    samples: tuple[Mapping[str, Any], ...]
    source_chunk_ids: tuple[str, ...]
    missing_chunk_ids: tuple[str, ...]

    @property
    def parquet_rows(self) -> list[tuple[object, ...]]:
        return list(self.rows)


def _chunk_envelope(artifact_body: bytes) -> dict[str, Any] | None:
    """Read the v2 envelope out of one ``validated/...`` artifact.

    ``GameplayConsumer`` writes ``{"artifact_version": 1, "chunk": <envelope>,
    ...}`` where ``chunk`` is the verbatim envelope, so the join input is
    ``artifact["chunk"]``.
    """
    try:
        artifact = json.loads(artifact_body.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError, ValueError, RecursionError):
        return None
    if not isinstance(artifact, Mapping):
        return None
    chunk = artifact.get("chunk")
    return dict(chunk) if isinstance(chunk, Mapping) else None


def build_rows(
    chunks: Sequence[Mapping[str, Any]],
    *,
    source_chunk_ids: Sequence[str] = (),
    missing_chunk_ids: Sequence[str] = (),
) -> EpisodeRowSet:
    """Run the ported BC join over ``chunks`` and project it onto the row schema."""
    try:
        samples = extract_behavior_cloning_samples(chunks)
    except (KeyError, TypeError, AttributeError) as error:
        raise EpisodeConversionError("validated chunk does not satisfy the BC join") from error
    return EpisodeRowSet(
        rows=tuple(samples_to_rows(samples)),
        samples=tuple(samples),
        source_chunk_ids=tuple(source_chunk_ids),
        missing_chunk_ids=tuple(missing_chunk_ids),
    )


async def load_episode_rows(store: R2Store, episode: EpisodeReady) -> EpisodeRowSet:
    """Read every validated chunk named by the message and project it into rows.

    A failing R2 read propagates so the Queue retries; an absent object does not.
    """
    source_chunk_ids: list[str] = []
    missing_chunk_ids: list[str] = []
    chunks: list[Mapping[str, Any]] = []

    for reference in sorted(episode.chunks, key=lambda item: item.chunk_sequence):
        key = validated_chunk_key(
            user_id=episode.user_id,
            game_id=episode.game_id,
            chunk_id=reference.chunk_id,
            created_at_ms=reference.created_at_ms,
        )
        stored = await store.get_raw(key)
        envelope = None if stored is None else _chunk_envelope(stored.body)
        if envelope is None:
            missing_chunk_ids.append(reference.chunk_id)
            continue
        source_chunk_ids.append(reference.chunk_id)
        chunks.append(envelope)

    return build_rows(
        chunks,
        source_chunk_ids=source_chunk_ids,
        missing_chunk_ids=missing_chunk_ids,
    )


__all__ = [
    "EPISODE_COLUMNS",
    "PARQUET_SCHEMA_VERSION",
    "EpisodeConversionError",
    "EpisodeRowSet",
    "build_rows",
    "canonical_json",
    "episode_id_for",
    "load_episode_rows",
    "require_game_type",
    "sample_to_row",
    "samples_to_rows",
    "utc_date_parts",
    "validated_chunk_key",
]
