"""Validate immutable gameplay chunks and write deterministic R2 artifacts."""

import hashlib
import json
import math
import re
import struct
import time
from collections.abc import Callable, Iterable, Mapping, Sequence
from datetime import UTC, datetime
from typing import Protocol, cast

from pydantic import ValidationError

from ..bindings import (
    EpisodeAssemblerNamespace,
    R2Bucket,
    is_binding_null,
    positive_int_field,
    value_field,
)
from ..contracts import (
    CHUNK_ID_PATTERN,
    GAME_ID_PATTERN,
    USER_ID_PATTERN,
    ChunkEnvelope,
    GameplayPointer,
    GameplayProcessResult,
    PermanentGameplayError,
    TransientGameplayError,
    parse_gameplay_chunk,
    safe_chunk_created_at_ms,
)
from ..episode_assembler import (
    DEFAULT_EPISODE_IDLE_TTL_MS,
    DEFAULT_EPISODE_MAX_CHUNKS,
    EpisodeAssemblerClient,
    EpisodeChunkRecord,
    episode_end_completed,
)
from ..storage.r2 import (
    DerivedObjectConflict,
    R2Store,
    require_r2_key,
    worker_buffer_to_bytes,
)

_POINTER_FIELDS = (
    "version",
    "r2_key",
    "payload_sha256",
    "user_id",
    "game_id",
    "chunk_id",
    "received_at_ms",
)
_IDENTIFIER_PATTERNS = {
    "user_id": re.compile(USER_ID_PATTERN),
    "game_id": re.compile(GAME_ID_PATTERN),
    "chunk_id": re.compile(CHUNK_ID_PATTERN),
}
_DIGEST_PATTERN = re.compile(r"^[0-9a-f]{64}$")
_POINTER_DIGEST_MAX_DEPTH = 2_048
_POINTER_DIGEST_MAX_NODES = 16_384
_MISSING = object()


class _ObjectKeysProxy(Protocol):
    def object_keys(self) -> Iterable[object]: ...


class _HashWriter(Protocol):
    def update(self, data: bytes, /) -> None: ...


def _current_time_ms() -> int:
    return time.time_ns() // 1_000_000


def _canonical_json(value: object) -> bytes:
    return json.dumps(
        value,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    ).encode("utf-8")


def _digest_field(hasher: _HashWriter, tag: bytes, payload: bytes = b"") -> None:
    hasher.update(len(tag).to_bytes(2, "big"))
    hasher.update(tag)
    hasher.update(len(payload).to_bytes(8, "big"))
    hasher.update(payload)


def _leaf_digest(tag: bytes, payload: bytes = b"") -> bytes:
    digest = hashlib.sha256()
    _digest_field(digest, tag, payload)
    return digest.digest()


def _primitive_digest(value: object) -> bytes | None:
    if value is None:
        return _leaf_digest(b"null")
    if isinstance(value, bool):
        return _leaf_digest(b"bool", b"1" if value else b"0")
    if isinstance(value, int):
        magnitude = abs(value)
        encoded = magnitude.to_bytes(max(1, (magnitude.bit_length() + 7) // 8), "big")
        return _leaf_digest(b"int-" if value < 0 else b"int+", encoded)
    if isinstance(value, float):
        if not math.isfinite(value):
            return _leaf_digest(b"nonfinite-number")
        return _leaf_digest(b"float", struct.pack(">d", value))
    if isinstance(value, str):
        encoded = value.encode("utf-8")
        return _leaf_digest(
            b"string",
            len(encoded).to_bytes(8, "big") + hashlib.sha256(encoded).digest(),
        )
    if isinstance(value, bytes):
        return _leaf_digest(
            b"bytes",
            len(value).to_bytes(8, "big") + hashlib.sha256(value).digest(),
        )
    if isinstance(value, (bytearray, memoryview)):
        encoded = bytes(value)
        return _leaf_digest(
            b"bytes",
            len(encoded).to_bytes(8, "big") + hashlib.sha256(encoded).digest(),
        )
    return None


def _container_ordinal(
    value: object,
    seen: dict[int, tuple[object, int]],
) -> tuple[int, bool]:
    identity = id(value)
    previous = seen.get(identity)
    if previous is not None:
        return previous[1], False
    ordinal = len(seen)
    seen[identity] = (value, ordinal)
    return ordinal, True


def _bounded_pointer_representation(value: object) -> bytes:
    digest = hashlib.sha256()
    # Strong values prevent identity-key reuse for the traversal lifetime.
    seen: dict[int, tuple[object, int]] = {}
    pending: list[tuple[int, object, int]] = [(0, value, 0)]
    visited_nodes = 0

    while pending:
        kind, item, depth = pending.pop()
        if kind == 1:
            _digest_field(digest, b"token", cast(bytes, item))
            continue
        if visited_nodes >= _POINTER_DIGEST_MAX_NODES:
            _digest_field(digest, b"node-limit", _POINTER_DIGEST_MAX_NODES.to_bytes(8, "big"))
            break
        visited_nodes += 1

        primitive = _primitive_digest(item)
        if primitive is not None:
            _digest_field(digest, b"leaf", primitive)
            continue
        if depth > _POINTER_DIGEST_MAX_DEPTH:
            _digest_field(digest, b"depth-limit", depth.to_bytes(8, "big"))
            continue

        if isinstance(item, Mapping):
            ordinal, is_new = _container_ordinal(item, seen)
            if not is_new:
                _digest_field(digest, b"reference", ordinal.to_bytes(8, "big"))
                continue
            remaining = _POINTER_DIGEST_MAX_NODES - visited_nodes
            entries: list[tuple[bytes, int, object]] = []
            try:
                declared_length = len(item)
                iterator = iter(item.items())
                for source_ordinal in range(remaining):
                    try:
                        key, child = next(iterator)
                    except StopIteration:
                        break
                    key_digest = _primitive_digest(key) or _leaf_digest(b"unsupported-key")
                    entries.append((key_digest, source_ordinal, child))
            except Exception:
                _digest_field(digest, b"mapping-read-error", ordinal.to_bytes(8, "big"))
                continue
            entries.sort(key=lambda entry: (entry[0], entry[1]))
            _digest_field(
                digest,
                b"mapping",
                ordinal.to_bytes(8, "big")
                + declared_length.to_bytes(8, "big")
                + len(entries).to_bytes(8, "big"),
            )
            pending.append((1, _leaf_digest(b"mapping-end", ordinal.to_bytes(8, "big")), depth))
            for key_digest, _source_ordinal, child in reversed(entries):
                pending.append((0, child, depth + 1))
                pending.append((1, key_digest, depth + 1))
            continue

        if isinstance(item, Sequence):
            ordinal, is_new = _container_ordinal(item, seen)
            if not is_new:
                _digest_field(digest, b"reference", ordinal.to_bytes(8, "big"))
                continue
            remaining = _POINTER_DIGEST_MAX_NODES - visited_nodes
            children: list[object] = []
            try:
                declared_length = len(item)
                child_count = min(declared_length, remaining)
                for index in range(child_count):
                    children.append(item[index])
            except Exception:
                _digest_field(digest, b"sequence-read-error", ordinal.to_bytes(8, "big"))
                continue
            _digest_field(
                digest,
                b"sequence",
                ordinal.to_bytes(8, "big")
                + declared_length.to_bytes(8, "big")
                + len(children).to_bytes(8, "big"),
            )
            pending.append((1, _leaf_digest(b"sequence-end", ordinal.to_bytes(8, "big")), depth))
            for child in reversed(children):
                pending.append((0, child, depth + 1))
            continue

        _digest_field(digest, b"unsupported-value")

    return _canonical_json(
        {
            "bounded_pointer_format": 1,
            "bounded_pointer_sha256": digest.hexdigest(),
        }
    )


def _canonical_pointer_representation(value: object) -> bytes:
    try:
        return _canonical_json(value)
    except (TypeError, ValueError, OverflowError, RecursionError):
        return _bounded_pointer_representation(value)


def _reject_pointer_constant(_value: str) -> None:
    raise ValueError("unsupported pointer JSON constant")


def _decode_pointer_json(encoded: bytes) -> object:
    try:
        return json.loads(
            encoded.decode("utf-8"),
            parse_constant=_reject_pointer_constant,
        )
    except (
        UnicodeDecodeError,
        json.JSONDecodeError,
        ValueError,
        RecursionError,
    ):
        return None


def _proxy_pointer_fields(value: object) -> dict[str, object]:
    try:
        attributes = vars(value)
    except TypeError:
        attributes = None
    if isinstance(attributes, Mapping):
        return dict(attributes)
    if hasattr(value, "object_keys"):
        proxy_fields: dict[str, object] = {}
        for key in cast(_ObjectKeysProxy, value).object_keys():
            if isinstance(key, str):
                proxy_fields[key] = value_field(value, key)
        return proxy_fields

    known_fields: dict[str, object] = {}
    for name in _POINTER_FIELDS:
        field = value_field(value, name, _MISSING)
        if field is not _MISSING:
            known_fields[name] = field
    return known_fields


def _normalize_pointer_body(
    value: object,
    buffer_to_bytes: Callable[[object], bytes],
) -> tuple[object, str]:
    if isinstance(value, bytes):
        representation = value
        data = _decode_pointer_json(value)
    elif isinstance(value, (bytearray, memoryview)):
        representation = bytes(value)
        data = _decode_pointer_json(representation)
    elif isinstance(value, str):
        representation = value.encode("utf-8")
        data = _decode_pointer_json(representation)
    elif isinstance(value, Mapping):
        data = dict(value)
        representation = _canonical_pointer_representation(data)
    else:
        try:
            representation = buffer_to_bytes(value)
        except (TypeError, ValueError):
            data = _proxy_pointer_fields(value)
            representation = _canonical_pointer_representation(data)
        else:
            data = _decode_pointer_json(representation)
    return data, hashlib.sha256(representation).hexdigest()


def _safe_pointer_fields(value: object) -> dict[str, object]:
    if not isinstance(value, Mapping):
        return {}
    safe: dict[str, object] = {}
    for field, pattern in _IDENTIFIER_PATTERNS.items():
        item = value.get(field)
        if isinstance(item, str) and pattern.fullmatch(item) is not None:
            safe[field] = item
    payload_sha256 = value.get("payload_sha256")
    if (
        isinstance(payload_sha256, str)
        and _DIGEST_PATTERN.fullmatch(payload_sha256) is not None
    ):
        safe["payload_sha256"] = payload_sha256
    received_at_ms = value.get("received_at_ms")
    if (
        not isinstance(received_at_ms, bool)
        and isinstance(received_at_ms, int)
        and received_at_ms >= 0
    ):
        safe["pointer_received_at_ms"] = received_at_ms
    return safe


def _stable_message_id(value: object | None) -> str | None:
    return value if isinstance(value, str) and value else None


def _stable_timestamp_ms(value: object | None) -> int | None:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        return None
    return value


def _utc_date(epoch_ms: int) -> str | None:
    try:
        return datetime.fromtimestamp(epoch_ms / 1000, UTC).date().isoformat()
    except (OverflowError, OSError, ValueError):
        return None


def _quarantine_digest(
    message_id: str | None,
    message_timestamp_ms: int,
    body_digest: str,
) -> str:
    material = f"body_sha256={body_digest}".encode("ascii")
    if message_id is not None:
        material = b"message_id=" + message_id.encode("utf-8") + b"\n" + material
    else:
        material = f"message_timestamp_ms={message_timestamp_ms}\n".encode() + material
    return hashlib.sha256(material).hexdigest()


def _build_assembler(environment: object) -> EpisodeAssemblerClient | None:
    """Build the episode assembler, or ``None`` while it is not bound.

    Episode assembly stays dormant until ``EPISODE_ASSEMBLER`` is declared, so
    this Worker keeps validating chunks unchanged on a deployment that has not
    provisioned the Durable Object and ``retro-episode-events`` yet.
    """
    namespace = value_field(environment, "EPISODE_ASSEMBLER")
    if is_binding_null(namespace):
        return None
    return EpisodeAssemblerClient(
        cast(EpisodeAssemblerNamespace, namespace),
        idle_ttl_ms=positive_int_field(
            environment,
            "EPISODE_IDLE_TTL_MS",
            DEFAULT_EPISODE_IDLE_TTL_MS,
        ),
        max_chunks=positive_int_field(
            environment,
            "EPISODE_MAX_CHUNKS",
            DEFAULT_EPISODE_MAX_CHUNKS,
        ),
    )


def _episode_ended(envelope: ChunkEnvelope) -> bool:
    return any(event.get("event_type") == "episode_ended" for event in envelope.events)


class GameplayConsumer:
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
            raise TransientGameplayError("gameplay storage unavailable")
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
        self.assembler = _build_assembler(environment)

    async def process(
        self,
        pointer_body: object,
        *,
        message_id: object | None = None,
        message_timestamp_ms: object | None = None,
    ) -> GameplayProcessResult:
        pointer_data, pointer_body_digest = _normalize_pointer_body(
            pointer_body,
            self._buffer_to_bytes,
        )
        stable_message_id = _stable_message_id(message_id)
        stable_message_timestamp_ms = _stable_timestamp_ms(message_timestamp_ms)
        try:
            pointer = GameplayPointer.model_validate(pointer_data)
        except (ValidationError, RecursionError):
            return await self._persist_invalid_pointer_rejection(
                pointer_data,
                pointer_body_digest,
                stable_message_id,
                stable_message_timestamp_ms,
            )

        try:
            raw_object = await self.store.get_raw(pointer.r2_key)
        except Exception as error:
            raise TransientGameplayError("raw object unavailable") from error
        if raw_object is None:
            raise TransientGameplayError("raw object unavailable")

        raw_body = raw_object.body
        observed_payload_sha256 = hashlib.sha256(raw_body).hexdigest()
        if raw_object.metadata_payload_sha256 != pointer.payload_sha256:
            metadata_payload_sha256 = raw_object.metadata_payload_sha256
            return await self._persist_pointer_rejection(
                pointer,
                raw_body,
                PermanentGameplayError(
                    "raw_metadata_hash_mismatch",
                    "raw object metadata hash does not match the pointer",
                ),
                observed_payload_sha256=observed_payload_sha256,
                metadata_payload_sha256=(
                    metadata_payload_sha256
                    if isinstance(metadata_payload_sha256, str)
                    and _DIGEST_PATTERN.fullmatch(metadata_payload_sha256) is not None
                    else None
                ),
            )
        if observed_payload_sha256 != pointer.payload_sha256:
            return await self._persist_pointer_rejection(
                pointer,
                raw_body,
                PermanentGameplayError(
                    "raw_payload_hash_mismatch",
                    "raw object bytes do not match the pointer hash",
                ),
                observed_payload_sha256=observed_payload_sha256,
            )

        try:
            envelope, chunk_data = parse_gameplay_chunk(raw_body, pointer.user_id)
        except PermanentGameplayError as error:
            return await self._persist_pointer_rejection(pointer, raw_body, error)

        if envelope.game_id != pointer.game_id or envelope.chunk_id != pointer.chunk_id:
            return await self._persist_pointer_rejection(
                pointer,
                raw_body,
                PermanentGameplayError(
                    "pointer_identity_mismatch",
                    "chunk identity does not match the queue pointer",
                ),
            )

        artifact_key = self._validated_key(pointer, envelope.created_at_ms)
        if artifact_key is None:
            return await self._persist_pointer_rejection(
                pointer,
                raw_body,
                PermanentGameplayError(
                    "invalid_chunk_contract",
                    "chunk does not satisfy the v2 event contract",
                ),
            )
        artifact = {
            "artifact_version": 1,
            "authenticated_user_id": pointer.user_id,
            "chunk": chunk_data,
            "payload_sha256": pointer.payload_sha256,
            "pointer_received_at_ms": pointer.received_at_ms,
        }
        artifact_body = _canonical_json(artifact)
        try:
            await self.store.put_derived(artifact_key, artifact_body)
        except DerivedObjectConflict:
            return await self._persist_pointer_rejection(
                pointer,
                raw_body,
                PermanentGameplayError(
                    "derived_object_conflict",
                    "validated artifact conflicts with an existing derived object",
                ),
            )
        except Exception as error:
            raise TransientGameplayError("validated artifact unavailable") from error

        await self._assemble_episode(pointer, envelope)
        return GameplayProcessResult(
            status="processed",
            artifact_key=artifact_key,
        )

    async def _assemble_episode(
        self,
        pointer: GameplayPointer,
        envelope: ChunkEnvelope,
    ) -> None:
        """Fold this chunk into its episode after its artifact is durable.

        Runs last so a retry always replays a chunk whose validated artifact
        already exists; the Durable Object drops the duplicate record.
        """
        if self.assembler is None:
            return
        try:
            await self.assembler.record(
                EpisodeChunkRecord(
                    chunk_id=envelope.chunk_id,
                    chunk_sequence=envelope.chunk_sequence,
                    first_event_sequence=envelope.first_event_sequence,
                    last_event_sequence=envelope.last_event_sequence,
                    created_at_ms=envelope.created_at_ms,
                    episode_ended=_episode_ended(envelope),
                    episode_completed=episode_end_completed(envelope.events),
                ),
                user_id=pointer.user_id,
                game_id=pointer.game_id,
                game_type=envelope.game_type,
                game_version=envelope.game_version,
                now_ms=self._clock_ms(),
            )
        except Exception as error:
            raise TransientGameplayError("episode assembly unavailable") from error

    @staticmethod
    def _validated_key(
        pointer: GameplayPointer,
        created_at_ms: int,
    ) -> str | None:
        partition_date = _utc_date(created_at_ms)
        if partition_date is None:
            return None
        key = (
            f"validated/date={partition_date}/user={pointer.user_id}"
            f"/game={pointer.game_id}/chunk={pointer.chunk_id}.json"
        )
        try:
            return require_r2_key(key)
        except ValueError:
            return None

    @staticmethod
    def _rejected_key(
        pointer: GameplayPointer,
        raw_body: bytes,
    ) -> str:
        created_at_ms = safe_chunk_created_at_ms(raw_body)
        partition_date = (
            _utc_date(created_at_ms) if created_at_ms is not None else None
        )
        if partition_date is None:
            partition_date = _utc_date(pointer.received_at_ms) or "1970-01-01"
        key = (
            f"rejected/date={partition_date}/user={pointer.user_id}"
            f"/game={pointer.game_id}/chunk={pointer.chunk_id}.json"
        )
        return require_r2_key(key)

    async def _persist_pointer_rejection(
        self,
        pointer: GameplayPointer,
        raw_body: bytes,
        error: PermanentGameplayError,
        *,
        observed_payload_sha256: str | None = None,
        metadata_payload_sha256: str | None = None,
    ) -> GameplayProcessResult:
        artifact_key = self._rejected_key(pointer, raw_body)
        artifact: dict[str, object] = {
            "artifact_version": 1,
            "chunk_id": pointer.chunk_id,
            "game_id": pointer.game_id,
            "payload_sha256": pointer.payload_sha256,
            "pointer_received_at_ms": pointer.received_at_ms,
            "reason": error.reason,
            "reason_code": error.reason_code,
            "rejection_at_ms": pointer.received_at_ms,
            "user_id": pointer.user_id,
        }
        if observed_payload_sha256 is not None:
            artifact["observed_payload_sha256"] = observed_payload_sha256
        if metadata_payload_sha256 is not None:
            artifact["metadata_payload_sha256"] = metadata_payload_sha256
        return await self._write_rejection(
            artifact_key,
            artifact,
            reason_code=error.reason_code,
        )

    async def _persist_invalid_pointer_rejection(
        self,
        pointer_data: object,
        pointer_body_digest: str,
        message_id: str | None,
        message_timestamp_ms: int | None,
    ) -> GameplayProcessResult:
        rejection_at_ms = message_timestamp_ms if message_timestamp_ms is not None else 0
        quarantine_sha256 = _quarantine_digest(
            message_id,
            rejection_at_ms,
            pointer_body_digest,
        )
        partition_date = _utc_date(rejection_at_ms) or "1970-01-01"
        artifact_key = require_r2_key(
            f"rejected/date={partition_date}/quarantine={quarantine_sha256}.json"
        )
        artifact: dict[str, object] = {
            "artifact_version": 1,
            "message_body_sha256": pointer_body_digest,
            "quarantine_sha256": quarantine_sha256,
            "reason": "queue message is not a valid gameplay pointer",
            "reason_code": "invalid_pointer",
            "rejection_at_ms": rejection_at_ms,
        }
        artifact.update(_safe_pointer_fields(pointer_data))
        if message_id is not None:
            artifact["message_id_sha256"] = hashlib.sha256(
                message_id.encode("utf-8")
            ).hexdigest()
        return await self._write_rejection(
            artifact_key,
            artifact,
            reason_code="invalid_pointer",
        )

    async def _write_rejection(
        self,
        artifact_key: str,
        artifact: Mapping[str, object],
        *,
        reason_code: str,
    ) -> GameplayProcessResult:
        try:
            await self.store.put_derived(artifact_key, _canonical_json(artifact))
        except Exception as error:
            raise TransientGameplayError("rejection artifact unavailable") from error
        return GameplayProcessResult(
            status="rejected",
            artifact_key=artifact_key,
            reason_code=reason_code,
        )


class GameplayDeadLetterConsumer:
    """Archive exhausted gameplay pointers before acknowledging the DLQ."""

    def __init__(
        self,
        environment: object,
        *,
        buffer_to_bytes: Callable[[object], bytes] = worker_buffer_to_bytes,
        to_js: Callable[[object], object] | None = None,
    ) -> None:
        bucket = value_field(environment, "RAW_BUCKET")
        if is_binding_null(bucket):
            raise TransientGameplayError("gameplay storage unavailable")
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

    async def process(
        self,
        pointer_body: object,
        *,
        message_id: object | None = None,
        message_timestamp_ms: object | None = None,
    ) -> str:
        pointer_data, pointer_body_digest = _normalize_pointer_body(
            pointer_body,
            self._buffer_to_bytes,
        )
        stable_message_id = _stable_message_id(message_id)
        stable_message_timestamp_ms = _stable_timestamp_ms(message_timestamp_ms)
        archive_at_ms = (
            stable_message_timestamp_ms
            if stable_message_timestamp_ms is not None
            else 0
        )
        archive_digest = _quarantine_digest(
            stable_message_id,
            archive_at_ms,
            pointer_body_digest,
        )
        partition_date = _utc_date(archive_at_ms) or "1970-01-01"
        artifact_key = require_r2_key(
            f"dlq/date={partition_date}/message={archive_digest}.json"
        )
        artifact: dict[str, object] = {
            "artifact_version": 1,
            "message_body_sha256": pointer_body_digest,
            "queue_message_timestamp_ms": archive_at_ms,
            "reason": "gameplay processing retries exhausted",
            "source_queue": "retro-gameplay-events",
        }
        artifact.update(_safe_pointer_fields(pointer_data))
        if stable_message_id is not None:
            artifact["message_id_sha256"] = hashlib.sha256(
                stable_message_id.encode("utf-8")
            ).hexdigest()
        try:
            await self.store.put_derived(artifact_key, _canonical_json(artifact))
        except Exception as error:
            raise TransientGameplayError("DLQ archive unavailable") from error
        return artifact_key


__all__ = [
    "GameplayConsumer",
    "GameplayDeadLetterConsumer",
    "PermanentGameplayError",
    "TransientGameplayError",
]
