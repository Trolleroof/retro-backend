"""Structural types and value normalization for Cloudflare bindings."""

from collections.abc import Iterable, Iterator, Mapping
from typing import Protocol, cast

_JS_NULL_TYPE_NAMES = frozenset({"JsNull"})


class D1PreparedStatement(Protocol):
    def bind(self, *values: object) -> "D1PreparedStatement": ...

    async def first(self) -> object | None: ...

    async def run(self) -> object: ...


class D1Database(Protocol):
    def prepare(self, sql: str) -> D1PreparedStatement: ...


class R2Bucket(Protocol):
    async def head(self, key: str) -> object | None: ...

    async def get(self, key: str) -> object | None: ...

    async def put(
        self,
        key: str,
        value: object,
        options: object | None = None,
    ) -> object | None: ...


class QueueBinding(Protocol):
    async def send(self, message: object) -> object: ...

    async def metrics(self) -> object: ...


class QueueMessage(Protocol):
    @property
    def id(self) -> object: ...

    @property
    def timestamp(self) -> object: ...

    @property
    def body(self) -> object: ...

    def ack(self) -> None: ...

    def retry(self) -> None: ...


class QueueBatch(Protocol):
    @property
    def queue(self) -> object: ...

    @property
    def messages(self) -> object: ...


class R2ObjectBody(Protocol):
    @property
    def customMetadata(self) -> object: ...

    async def arrayBuffer(self) -> object: ...


class RateLimiterBinding(Protocol):
    async def limit(self, options: dict[str, str]) -> object: ...


class ChunkGateStub(Protocol):
    async def reserve(
        self,
        payload_sha256: str,
        lease_token: str,
        now_ms: int,
        lease_ms: int,
    ) -> object: ...

    async def accept(
        self,
        payload_sha256: str,
        lease_token: str,
        acknowledgment: object,
        now_ms: int,
        dedupe_ttl_ms: int,
    ) -> object: ...

    async def release(self, payload_sha256: str, lease_token: str) -> object: ...


class EpisodeAssemblerStub(Protocol):
    async def record(
        self,
        chunk: object,
        now_ms: int,
        idle_ttl_ms: int,
        max_chunks: int,
    ) -> object: ...


class DurableObjectNamespace(Protocol):
    def idFromName(self, name: str) -> object: ...

    def get(self, object_id: object) -> ChunkGateStub: ...


class EpisodeAssemblerNamespace(Protocol):
    def idFromName(self, name: str) -> object: ...

    def get(self, object_id: object) -> EpisodeAssemblerStub: ...


class DurableObjectStorage(Protocol):
    async def get(self, key: str) -> object | None: ...

    async def put(self, key: str, value: object) -> object: ...

    async def setAlarm(self, scheduled_time_ms: int) -> object: ...

    async def deleteAlarm(self) -> object: ...

    async def deleteAll(self) -> object: ...


class DurableObjectContext(Protocol):
    @property
    def storage(self) -> DurableObjectStorage: ...


def is_binding_null(value: object) -> bool:
    """Recognize Python ``None`` and Pyodide's JavaScript null sentinel."""
    if value is None:
        return True
    return (
        type(value).__name__ in _JS_NULL_TYPE_NAMES
        and getattr(value, "typeof", None) == "object"
        and repr(value) == "jsnull"
    )


def value_field(value: object, name: str, default: object | None = None) -> object | None:
    """Read a field from a Python mapping or a binding-proxy attribute."""
    if is_binding_null(value):
        return default
    if isinstance(value, Mapping):
        field = value.get(name, default)
    else:
        field = getattr(value, name, default)
    return default if is_binding_null(field) else field


def iter_binding_values(value: object) -> Iterator[object]:
    """Iterate list-like binding values without depending on Pyodide types."""
    if is_binding_null(value) or isinstance(value, (str, bytes, bytearray, Mapping)):
        return iter(())
    try:
        return iter(cast(Iterable[object], value))
    except TypeError:
        return iter(())


def positive_int_field(environment: object, name: str, default: int) -> int:
    """Read one positive integer config value without coercing booleans or floats."""
    value = value_field(environment, name)
    if isinstance(value, bool):
        return default
    if isinstance(value, int):
        return value if value > 0 else default
    if isinstance(value, str) and value.isascii() and value.isdecimal():
        try:
            parsed = int(value)
        except ValueError:
            return default
        return parsed if parsed > 0 else default
    return default
