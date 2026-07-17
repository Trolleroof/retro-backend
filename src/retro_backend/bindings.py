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
    async def get(self, key: str) -> object | None: ...

    async def put(
        self,
        key: str,
        value: object,
        options: object | None = None,
    ) -> object: ...


class QueueBinding(Protocol):
    async def send(self, message: object) -> object: ...


class DurableObjectNamespace(Protocol):
    def idFromName(self, name: str) -> object: ...

    def get(self, object_id: object) -> object: ...


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
