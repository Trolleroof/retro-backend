"""Structural Cloudflare binding types used by multiplayer Durable Objects."""

from collections.abc import Awaitable, Callable, Iterable, Iterator, Mapping
from typing import Protocol

type SqlRow = Mapping[str, object]


class SqlCursor(Protocol):
    def __iter__(self) -> Iterator[SqlRow]: ...


class RoomSql(Protocol):
    def exec(self, statement: str, *bindings: object) -> SqlCursor: ...


class RoomStorage(Protocol):
    @property
    def sql(self) -> RoomSql: ...

    def transactionSync[ResultT](self, callback: Callable[[], ResultT]) -> ResultT: ...

    async def getAlarm(self) -> int | None: ...

    async def setAlarm(self, scheduled_time_ms: int) -> object: ...

    async def deleteAll(self) -> object: ...


class RoomHeaders(Protocol):
    def get(self, name: str) -> str | None: ...


class RoomRequest(Protocol):
    @property
    def url(self) -> str: ...

    @property
    def method(self) -> str: ...

    @property
    def headers(self) -> RoomHeaders: ...


class RoomWebSocket(Protocol):
    @property
    def js_id(self) -> int: ...

    @property
    def readyState(self) -> int: ...

    def send(self, payload: str) -> None: ...

    def close(self, code: int, reason: str) -> None: ...

    def serializeAttachment(self, attachment: str) -> None: ...

    def deserializeAttachment(self) -> object: ...


class RoomWebSocketPair(Protocol):
    def object_values(self) -> tuple[RoomWebSocket, RoomWebSocket]: ...


class RoomWebSocketPairFactory(Protocol):
    def new(self) -> RoomWebSocketPair: ...


class RoomContext(Protocol):
    @property
    def storage(self) -> RoomStorage: ...

    def acceptWebSocket(self, socket: RoomWebSocket) -> None: ...

    def getWebSockets(self) -> Iterable[RoomWebSocket]: ...

    def blockConcurrencyWhile(
        self,
        callback: Callable[[], Awaitable[object]],
    ) -> object: ...


__all__ = [
    "RoomContext",
    "RoomHeaders",
    "RoomRequest",
    "RoomSql",
    "RoomStorage",
    "RoomWebSocket",
    "RoomWebSocketPair",
    "RoomWebSocketPairFactory",
    "SqlCursor",
    "SqlRow",
]
