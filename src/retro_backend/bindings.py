"""Structural types for Cloudflare bindings used by pure-Python adapters."""

from collections.abc import Mapping
from typing import Protocol


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


def value_field(value: object, name: str, default: object | None = None) -> object | None:
    """Read a field from a Python mapping or a binding-proxy attribute."""
    if isinstance(value, Mapping):
        return value.get(name, default)
    return getattr(value, name, default)
