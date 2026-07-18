"""Deterministic R2 keys and conditional exact-byte writes."""

import hashlib
import re
from collections.abc import Callable
from dataclasses import dataclass
from importlib import import_module
from typing import cast

from ..bindings import R2Bucket, R2ObjectBody, is_binding_null, value_field
from ..contracts import CHUNK_ID_PATTERN, GAME_ID_PATTERN, USER_ID_PATTERN

R2_MAX_KEY_BYTES = 1_024


class ObjectConflict(RuntimeError):
    """Raised when a deterministic R2 key already owns different bytes."""


class DerivedObjectConflict(RuntimeError):
    """Raised when a deterministic derived key owns a different artifact."""


def require_r2_key(key: str) -> str:
    if len(key.encode("utf-8")) > R2_MAX_KEY_BYTES:
        raise ValueError("R2 key must not exceed 1024 bytes")
    return key


def _require_identifier(name: str, value: str, pattern: str) -> str:
    if re.fullmatch(pattern, value) is None:
        raise ValueError(f"{name} has an invalid format")
    return value


def _worker_to_js(value: object) -> object:
    """Convert native Python values at the Pyodide/Workers binding boundary."""
    try:
        js_module = import_module("js")
        ffi_module = import_module("pyodide.ffi")
    except ModuleNotFoundError:
        return value
    return ffi_module.to_js(
        value,
        dict_converter=js_module.Object.fromEntries,
    )


def worker_buffer_to_bytes(value: object) -> bytes:
    """Copy a Workers ArrayBuffer/typed array into exact Python bytes."""
    if isinstance(value, bytes):
        return value
    if isinstance(value, (bytearray, memoryview)):
        return bytes(value)
    to_bytes = getattr(value, "to_bytes", None)
    if callable(to_bytes):
        converted = to_bytes()
        if isinstance(converted, bytes):
            return converted
        if isinstance(converted, (bytearray, memoryview)):
            return bytes(converted)
    raise TypeError("R2 object body is not a supported byte buffer")


@dataclass(frozen=True, slots=True)
class RawObjectIdentity:
    user_id: str
    game_id: str
    chunk_id: str

    @property
    def key(self) -> str:
        return R2Store.raw_key(
            user_id=self.user_id,
            game_id=self.game_id,
            chunk_id=self.chunk_id,
        )

    def custom_metadata(self, payload_sha256: str) -> dict[str, str]:
        return {
            "payload_sha256": payload_sha256,
            "schema_version": "2",
            "user_id": self.user_id,
            "game_id": self.game_id,
            "chunk_id": self.chunk_id,
        }


@dataclass(frozen=True, slots=True)
class RawObjectData:
    body: bytes
    metadata_payload_sha256: object | None


class R2Store:
    def __init__(
        self,
        bucket: R2Bucket,
        *,
        to_js: Callable[[object], object] = _worker_to_js,
        buffer_to_bytes: Callable[[object], bytes] = worker_buffer_to_bytes,
    ) -> None:
        self.bucket = bucket
        self._to_js = to_js
        self._buffer_to_bytes = buffer_to_bytes

    @staticmethod
    def raw_key(*, user_id: str, game_id: str, chunk_id: str) -> str:
        user_id = _require_identifier("user_id", user_id, USER_ID_PATTERN)
        game_id = _require_identifier("game_id", game_id, GAME_ID_PATTERN)
        chunk_id = _require_identifier("chunk_id", chunk_id, CHUNK_ID_PATTERN)
        return require_r2_key(
            f"raw/user={user_id}/game={game_id}/chunk={chunk_id}.json"
        )

    @staticmethod
    def _require_matching_hash(stored: object, payload_sha256: str) -> None:
        custom_metadata = value_field(stored, "customMetadata")
        stored_digest = value_field(custom_metadata, "payload_sha256")
        if stored_digest != payload_sha256:
            raise ObjectConflict("raw object contains different bytes")

    @staticmethod
    def _require_matching_derived_hash(
        stored: object,
        payload_sha256: str,
    ) -> None:
        custom_metadata = value_field(stored, "customMetadata")
        stored_digest = value_field(custom_metadata, "payload_sha256")
        if stored_digest != payload_sha256:
            raise DerivedObjectConflict("derived object contains different bytes")

    async def get_raw(self, key: str) -> RawObjectData | None:
        key = require_r2_key(key)
        stored = await self.bucket.get(key)
        if is_binding_null(stored):
            return None
        object_body = cast(R2ObjectBody, stored)
        buffer = await object_body.arrayBuffer()
        body = self._buffer_to_bytes(buffer)
        custom_metadata = value_field(stored, "customMetadata")
        return RawObjectData(
            body=body,
            metadata_payload_sha256=value_field(
                custom_metadata,
                "payload_sha256",
            ),
        )

    async def _put_if_absent(
        self,
        *,
        key: str,
        body: bytes,
        payload_sha256: str,
        custom_metadata: dict[str, str],
        require_matching_hash: Callable[[object, str], None],
    ) -> str:
        key = require_r2_key(key)
        existing = await self.bucket.head(key)
        if not is_binding_null(existing):
            require_matching_hash(existing, payload_sha256)
            return key

        options = {
            "onlyIf": {"etagDoesNotMatch": "*"},
            "sha256": payload_sha256,
            "httpMetadata": {"contentType": "application/json"},
            "customMetadata": custom_metadata,
        }
        body_value = self._to_js(body)
        options_value = self._to_js(options)
        try:
            result = await self.bucket.put(key, body_value, options_value)
        except Exception as error:
            try:
                race_winner = await self.bucket.head(key)
            except Exception as recovery_error:
                raise error from recovery_error
            if is_binding_null(race_winner):
                raise
            require_matching_hash(race_winner, payload_sha256)
            return key

        if is_binding_null(result):
            race_winner = await self.bucket.head(key)
            if is_binding_null(race_winner):
                raise RuntimeError("R2 conditional write failed")
            require_matching_hash(race_winner, payload_sha256)
        return key

    async def put_raw(
        self,
        *,
        user_id: str,
        game_id: str,
        chunk_id: str,
        raw_body: bytes,
        payload_sha256: str,
    ) -> str:
        identity = RawObjectIdentity(
            user_id=user_id,
            game_id=game_id,
            chunk_id=chunk_id,
        )
        return await self._put_if_absent(
            key=identity.key,
            body=raw_body,
            payload_sha256=payload_sha256,
            custom_metadata=identity.custom_metadata(payload_sha256),
            require_matching_hash=self._require_matching_hash,
        )

    async def put_derived(self, key: str, body: bytes) -> str:
        payload_sha256 = hashlib.sha256(body).hexdigest()
        return await self._put_if_absent(
            key=key,
            body=body,
            payload_sha256=payload_sha256,
            custom_metadata={"payload_sha256": payload_sha256},
            require_matching_hash=self._require_matching_derived_hash,
        )
