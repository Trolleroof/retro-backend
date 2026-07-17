"""Deterministic R2 keys and conditional exact-byte writes."""

from collections.abc import Callable
from dataclasses import dataclass
from importlib import import_module

from ..bindings import R2Bucket, is_binding_null, value_field


class ObjectConflict(RuntimeError):
    """Raised when a deterministic R2 key already owns different bytes."""


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


class R2Store:
    def __init__(
        self,
        bucket: R2Bucket,
        *,
        to_js: Callable[[object], object] = _worker_to_js,
    ) -> None:
        self.bucket = bucket
        self._to_js = to_js

    @staticmethod
    def raw_key(*, user_id: str, game_id: str, chunk_id: str) -> str:
        return f"raw/user={user_id}/game={game_id}/chunk={chunk_id}.json"

    @staticmethod
    def _require_matching_hash(stored: object, payload_sha256: str) -> None:
        custom_metadata = value_field(stored, "customMetadata")
        stored_digest = value_field(custom_metadata, "payload_sha256")
        if stored_digest != payload_sha256:
            raise ObjectConflict("raw object contains different bytes")

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
        key = identity.key
        existing = await self.bucket.head(key)
        if not is_binding_null(existing):
            self._require_matching_hash(existing, payload_sha256)
            return key

        options = {
            "onlyIf": {"etagDoesNotMatch": "*"},
            "sha256": payload_sha256,
            "httpMetadata": {"contentType": "application/json"},
            "customMetadata": identity.custom_metadata(payload_sha256),
        }
        body_value = self._to_js(raw_body)
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
            self._require_matching_hash(race_winner, payload_sha256)
            return key

        if is_binding_null(result):
            race_winner = await self.bucket.head(key)
            if is_binding_null(race_winner):
                raise RuntimeError("R2 conditional write failed")
            self._require_matching_hash(race_winner, payload_sha256)
        return key
