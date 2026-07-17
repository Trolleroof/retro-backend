"""Validate immutable gameplay chunks and write deterministic R2 artifacts."""

import hashlib
import json
import math
import re
import time
from collections.abc import Callable, Iterable, Mapping
from datetime import UTC, datetime
from typing import Protocol, cast

from pydantic import ValidationError

from ..bindings import R2Bucket, is_binding_null, value_field
from ..contracts import (
    GameplayPointer,
    GameplayProcessResult,
    PermanentGameplayError,
    TransientGameplayError,
    parse_gameplay_chunk,
    safe_chunk_created_at_ms,
)
from ..storage.r2 import DerivedObjectConflict, R2Store, worker_buffer_to_bytes

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
    "user_id": re.compile(r"^usr_[A-Za-z0-9._-]+$"),
    "game_id": re.compile(r"^game_[A-Za-z0-9._-]+$"),
    "chunk_id": re.compile(r"^chk_[A-Za-z0-9._-]+$"),
}
_DIGEST_PATTERN = re.compile(r"^[0-9a-f]{64}$")
_MISSING = object()


class _ObjectKeysProxy(Protocol):
    def object_keys(self) -> Iterable[object]: ...


def _current_time_ms() -> int:
    return time.time_ns() // 1_000_000


def _canonical_json(value: object) -> bytes:
    return json.dumps(
        value,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    ).encode("utf-8")


def _safe_digest_value(value: object, active: set[int] | None = None) -> object:
    if value is None or isinstance(value, (bool, int, str)):
        return value
    if isinstance(value, float):
        return value if math.isfinite(value) else {"type": "nonfinite-number"}
    if isinstance(value, bytes):
        return {"bytes_sha256": hashlib.sha256(value).hexdigest()}
    if isinstance(value, (bytearray, memoryview)):
        body = bytes(value)
        return {"bytes_sha256": hashlib.sha256(body).hexdigest()}
    if isinstance(value, (Mapping, list, tuple)):
        active = active if active is not None else set()
        identity = id(value)
        if identity in active:
            return {"type": "cycle"}
        active.add(identity)
        try:
            if isinstance(value, Mapping):
                return {
                    key: _safe_digest_value(item, active)
                    for key, item in value.items()
                    if isinstance(key, str)
                }
            return [_safe_digest_value(item, active) for item in value]
        finally:
            active.remove(identity)
    return {"type": type(value).__name__}


def _canonical_pointer_representation(value: object) -> bytes:
    try:
        return _canonical_json(value)
    except (TypeError, ValueError, OverflowError, RecursionError):
        return _canonical_json(_safe_digest_value(value))


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
        except ValidationError:
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
        return GameplayProcessResult(
            status="processed",
            artifact_key=artifact_key,
        )

    @staticmethod
    def _validated_key(
        pointer: GameplayPointer,
        created_at_ms: int,
    ) -> str | None:
        partition_date = _utc_date(created_at_ms)
        if partition_date is None:
            return None
        return (
            f"validated/date={partition_date}/user={pointer.user_id}"
            f"/game={pointer.game_id}/chunk={pointer.chunk_id}.json"
        )

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
        return (
            f"rejected/date={partition_date}/user={pointer.user_id}"
            f"/game={pointer.game_id}/chunk={pointer.chunk_id}.json"
        )

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
        artifact_key = (
            f"rejected/date={partition_date}"
            f"/quarantine={quarantine_sha256}.json"
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


__all__ = [
    "GameplayConsumer",
    "PermanentGameplayError",
    "TransientGameplayError",
]
