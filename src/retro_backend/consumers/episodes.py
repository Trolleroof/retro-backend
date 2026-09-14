"""Turn one ``episode.ready`` message into Parquet, a manifest, and a D1 row.

Ordering is load-bearing: both R2 objects are written before the D1 row, so the
index never advertises an artifact that does not exist. Every write is
idempotent under Queue retry — the keys are deterministic and the puts overwrite
— which is what assembly's at-least-once publishing requires.
"""

import hashlib
import json
import time
from collections.abc import Callable, Mapping
from typing import Literal, cast

from pydantic import BaseModel, ConfigDict, Field, ValidationError

from ..bindings import (
    D1Database,
    R2Bucket,
    is_binding_null,
    iter_binding_values,
    value_field,
)
from ..contracts import EpisodeReady, TransientGameplayError
from ..episode_rows import (
    EPISODE_COLUMNS,
    EpisodeConversionError,
    EpisodeRowSet,
    canonical_json,
    episode_id_for,
    load_episode_rows,
    require_game_type,
    utc_date_parts,
)
from ..parquet import write_parquet
from ..storage.d1 import D1Repository, EpisodeIndexRow
from ..storage.r2 import R2Store, require_r2_key, worker_buffer_to_bytes

EPISODE_PARQUET_PREFIX = "episodes/parquet/v1"
MANIFEST_VERSION = 1

_EPISODE_FIELDS = (
    "version",
    "status",
    "user_id",
    "game_id",
    "game_type",
    "game_version",
    "chunk_count",
    "first_event_sequence",
    "last_event_sequence",
    "completed_at_ms",
)
_CHUNK_FIELDS = ("chunk_id", "chunk_sequence", "created_at_ms")


def _current_time_ms() -> int:
    return time.time_ns() // 1_000_000


class EpisodeWriteResult(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    status: Literal["written", "invalid"]
    episode_id: str | None = None
    parquet_key: str | None = None
    manifest_key: str | None = None
    row_count: int = Field(default=0, ge=0)


def _log_invalid_message(message_id: object | None, reason: str) -> None:
    """Log a poison message without echoing any part of its body."""
    message_digest = (
        hashlib.sha256(message_id.encode("utf-8")).hexdigest()
        if isinstance(message_id, str)
        else None
    )
    print(json.dumps({
        "event": "episode_message_invalid",
        "queue": "retro-episode-events",
        "message_id_sha256": message_digest,
        "reason": reason,
    }, separators=(",", ":")))


def _log_written(result: "EpisodeWriteResult", *, status: str, parquet_bytes: int) -> None:
    print(json.dumps({
        "event": "episode_parquet_written",
        "episode_id": result.episode_id,
        "episode_status": status,
        "row_count": result.row_count,
        "parquet_bytes": parquet_bytes,
    }, separators=(",", ":")))


def _proxy_chunks(value: object) -> list[dict[str, object | None]]:
    return [
        {field: value_field(item, field) for field in _CHUNK_FIELDS}
        for item in iter_binding_values(value)
    ]


def _episode_message_data(body: object, buffer_to_bytes: Callable[[object], bytes]) -> object:
    """Normalize a structured-clone mapping, JSON text, or Pyodide proxy."""
    if isinstance(body, (bytes, bytearray, memoryview)):
        return _decode_json(bytes(body))
    if isinstance(body, str):
        return _decode_json(body.encode("utf-8"))
    if isinstance(body, Mapping):
        return dict(body)
    try:
        return _decode_json(buffer_to_bytes(body))
    except (TypeError, ValueError):
        data: dict[str, object | None] = {
            field: value_field(body, field) for field in _EPISODE_FIELDS
        }
        data["chunks"] = _proxy_chunks(value_field(body, "chunks"))
        return data


def _decode_json(encoded: bytes) -> object:
    try:
        return json.loads(encoded.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError, ValueError, RecursionError):
        return None


def episode_parquet_key(*, game_type: str, game_id: str, completed_at_ms: int) -> str:
    year, month, day = utc_date_parts(completed_at_ms)
    partition = f"{require_game_type(game_type)}/{year}/{month}/{day}"
    return require_r2_key(f"{EPISODE_PARQUET_PREFIX}/{partition}/{game_id}.parquet")


def episode_manifest_key(parquet_key: str) -> str:
    return require_r2_key(f"{parquet_key.removesuffix('.parquet')}.manifest.json")


def build_manifest(
    episode: EpisodeReady,
    rows: EpisodeRowSet,
    *,
    episode_id: str,
    parquet_key: str,
    parquet_body: bytes,
    written_at_ms: int,
) -> dict[str, object]:
    return {
        "version": MANIFEST_VERSION,
        "episode_id": episode_id,
        "user_id": episode.user_id,
        "game_id": episode.game_id,
        "game_type": episode.game_type,
        "game_version": episode.game_version,
        "status": episode.status,
        "chunk_count": episode.chunk_count,
        "row_count": len(rows.rows),
        "first_event_sequence": episode.first_event_sequence,
        "last_event_sequence": episode.last_event_sequence,
        "completed_at_ms": episode.completed_at_ms,
        "parquet_key": parquet_key,
        "parquet_bytes": len(parquet_body),
        "parquet_sha256": hashlib.sha256(parquet_body).hexdigest(),
        "source_chunk_ids": list(rows.source_chunk_ids),
        # Named explicitly rather than left to be inferred from the gap between
        # chunk_count and source_chunk_ids: an incomplete or abandoned episode
        # legitimately writes a Parquet short of rows, and a reader needs to
        # tell "this episode had gaps" from "the join emitted nothing".
        "missing_chunk_ids": list(rows.missing_chunk_ids),
        "written_at_ms": written_at_ms,
    }


class EpisodeConsumer:
    """Consume ``retro-episode-events`` and materialize the episode dataset."""

    def __init__(
        self,
        environment: object,
        *,
        clock_ms: Callable[[], int] = _current_time_ms,
        buffer_to_bytes: Callable[[object], bytes] = worker_buffer_to_bytes,
        to_js: Callable[[object], object] | None = None,
    ) -> None:
        bucket = value_field(environment, "RAW_BUCKET")
        if is_binding_null(bucket):
            raise TransientGameplayError("episode storage unavailable")
        database = value_field(environment, "DB")
        if is_binding_null(database):
            raise TransientGameplayError("episode index unavailable")
        self._clock_ms = clock_ms
        self._buffer_to_bytes = buffer_to_bytes
        if to_js is None:
            self.store = R2Store(
                cast(R2Bucket, bucket),
                buffer_to_bytes=buffer_to_bytes,
            )
        else:
            self.store = R2Store(
                cast(R2Bucket, bucket),
                buffer_to_bytes=buffer_to_bytes,
                to_js=to_js,
            )
        self.repository = D1Repository(cast(D1Database, database))

    async def process(
        self,
        message_body: object,
        *,
        message_id: object | None = None,
        message_timestamp_ms: object | None = None,
    ) -> EpisodeWriteResult:
        _ = message_timestamp_ms
        data = _episode_message_data(message_body, self._buffer_to_bytes)
        try:
            episode = EpisodeReady.model_validate(data)
        except (ValidationError, RecursionError):
            # A message this Worker can never satisfy: acknowledge rather than
            # burn five retries on the way to a dead-letter queue.
            _log_invalid_message(message_id, "invalid_episode_message")
            return EpisodeWriteResult(status="invalid")
        try:
            parquet_key = episode_parquet_key(
                game_type=episode.game_type,
                game_id=episode.game_id,
                completed_at_ms=episode.completed_at_ms,
            )
        except (ValueError, OSError, OverflowError):
            _log_invalid_message(message_id, "unroutable_episode_identity")
            return EpisodeWriteResult(status="invalid")

        try:
            rows = await load_episode_rows(self.store, episode)
            parquet_body = write_parquet(EPISODE_COLUMNS, rows.parquet_rows)
        except EpisodeConversionError:
            # The artifacts are durable and malformed; retrying cannot fix them.
            _log_invalid_message(message_id, "unconvertible_episode_chunks")
            return EpisodeWriteResult(status="invalid")
        manifest_key = episode_manifest_key(parquet_key)
        episode_id = episode_id_for(episode.user_id, episode.game_id)
        written_at_ms = self._clock_ms()
        manifest = build_manifest(
            episode,
            rows,
            episode_id=episode_id,
            parquet_key=parquet_key,
            parquet_body=parquet_body,
            written_at_ms=written_at_ms,
        )

        # R2 first, then D1: the index must never name an object that is absent.
        try:
            await self.store.put_object(
                parquet_key,
                parquet_body,
                content_type="application/vnd.apache.parquet",
            )
            await self.store.put_object(
                manifest_key,
                canonical_json(manifest).encode("utf-8"),
                content_type="application/json",
            )
        except Exception as error:
            raise TransientGameplayError("episode artifact unavailable") from error

        try:
            await self.repository.upsert_episode(
                EpisodeIndexRow(
                    episode_id=episode_id,
                    user_id=episode.user_id,
                    game_id=episode.game_id,
                    game_type=episode.game_type,
                    game_version=episode.game_version,
                    status=episode.status,
                    chunk_count=episode.chunk_count,
                    row_count=len(rows.rows),
                    first_event_sequence=episode.first_event_sequence,
                    last_event_sequence=episode.last_event_sequence,
                    completed_at_ms=episode.completed_at_ms,
                    parquet_key=parquet_key,
                    parquet_bytes=len(parquet_body),
                    parquet_sha256=hashlib.sha256(parquet_body).hexdigest(),
                    manifest_key=manifest_key,
                    written_at_ms=written_at_ms,
                )
            )
        except Exception as error:
            raise TransientGameplayError("episode index unavailable") from error

        result = EpisodeWriteResult(
            status="written",
            episode_id=episode_id,
            parquet_key=parquet_key,
            manifest_key=manifest_key,
            row_count=len(rows.rows),
        )
        _log_written(result, status=episode.status, parquet_bytes=len(parquet_body))
        return result


__all__ = [
    "EPISODE_PARQUET_PREFIX",
    "EpisodeConsumer",
    "EpisodeWriteResult",
    "build_manifest",
    "episode_manifest_key",
    "episode_parquet_key",
]
