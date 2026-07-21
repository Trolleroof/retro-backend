"""SQLite-backed fakes for the multiplayer Durable Object boundary."""

from __future__ import annotations

import inspect
import json
import re
import sqlite3
from collections.abc import Awaitable, Callable, Iterator, Mapping
from dataclasses import dataclass, field
from typing import cast, override

type SqlRow = Mapping[str, object]


@dataclass(slots=True)
class FakeSqlCursor(Iterator[SqlRow]):
    """Cloudflare-shaped cursor that is consumed only after StopIteration."""

    rows: tuple[SqlRow, ...]
    rowsWritten: int
    consumed: bool = field(default=False, init=False)
    _index: int = field(default=0, init=False, repr=False)

    @override
    def __iter__(self) -> FakeSqlCursor:
        return self

    @override
    def __next__(self) -> SqlRow:
        if self._index >= len(self.rows):
            self.consumed = True
            raise StopIteration
        row = self.rows[self._index]
        self._index += 1
        return row


@dataclass(frozen=True, slots=True)
class SqlExecution:
    statement: str
    normalized_sql: str
    bindings: tuple[object, ...]
    cursor: FakeSqlCursor | None
    failed: bool = False


@dataclass(slots=True)
class _SqlFailure:
    normalized_sql: str
    occurrence: int
    seen: int = 0


def normalize_sql(statement: str) -> str:
    normalized = " ".join(statement.split())
    normalized = re.sub(r"\(\s+", "(", normalized)
    return re.sub(r"\s+\)", ")", normalized)


class FakeRoomSql:
    def __init__(self, storage: FakeRoomStorage) -> None:
        self._storage = storage

    def exec(self, statement: str, *bindings: object) -> FakeSqlCursor:
        normalized_sql = normalize_sql(statement)
        self._storage.events.append(f"sql:{normalized_sql}")
        fail_after_execution = self._storage.consume_sql_failure(normalized_sql)
        if (
            self._storage.fail_next_action_insert
            and normalized_sql.upper().startswith("INSERT INTO ACTIONS")
        ):
            self._storage.fail_next_action_insert = False
            self._storage.executions.append(
                SqlExecution(
                    statement=statement,
                    normalized_sql=normalized_sql,
                    bindings=bindings,
                    cursor=None,
                    failed=True,
                )
            )
            raise sqlite3.OperationalError("injected action insert failure")

        before = self._storage.connection.total_changes
        try:
            sqlite_cursor = self._storage.connection.execute(statement, bindings)
            description = sqlite_cursor.description
            raw_rows = sqlite_cursor.fetchall()
        except sqlite3.Error:
            self._storage.executions.append(
                SqlExecution(
                    statement=statement,
                    normalized_sql=normalized_sql,
                    bindings=bindings,
                    cursor=None,
                    failed=True,
                )
            )
            raise

        names = () if description is None else tuple(column[0] for column in description)
        rows = tuple(
            cast(SqlRow, dict(zip(names, raw_row, strict=True)))
            for raw_row in raw_rows
        )
        rows_written = self._storage.connection.total_changes - before
        if self._storage.inflate_rows_written and rows_written > 0:
            rows_written += 100
        cursor = FakeSqlCursor(rows=rows, rowsWritten=rows_written)
        self._storage.cursors.append(cursor)
        if fail_after_execution:
            list(cursor)
            self._storage.executions.append(
                SqlExecution(
                    statement=statement,
                    normalized_sql=normalized_sql,
                    bindings=bindings,
                    cursor=cursor,
                    failed=True,
                )
            )
            raise sqlite3.OperationalError("injected SQL failure")
        self._storage.executions.append(
            SqlExecution(
                statement=statement,
                normalized_sql=normalized_sql,
                bindings=bindings,
                cursor=cursor,
            )
        )
        return cursor


class FakeRoomStorage:
    """Real in-memory SQLite with Cloudflare's synchronous transaction shape."""

    def __init__(self, *, inflate_rows_written: bool = False) -> None:
        self.connection = sqlite3.connect(":memory:", isolation_level=None)
        self.sql = FakeRoomSql(self)
        self.inflate_rows_written = inflate_rows_written
        self.executions: list[SqlExecution] = []
        self.cursors: list[FakeSqlCursor] = []
        self.events: list[str] = []
        self.transaction_events: list[str] = []
        self.fail_next_action_insert = False
        self._sql_failures: list[_SqlFailure] = []
        self.alarm_at_ms: int | None = None
        self.alarm_events: list[tuple[str, int | None]] = []
        self.delete_all_count = 0
        self.fail_next_set_alarm = False
        self.fail_next_delete_all = False

    def transactionSync[ResultT](self, callback: Callable[[], ResultT]) -> ResultT:
        if inspect.iscoroutinefunction(callback):
            raise TypeError("transactionSync callback must be synchronous")
        if self.connection.in_transaction:
            raise RuntimeError("nested transactionSync is not supported by the fake")

        self.connection.execute("BEGIN IMMEDIATE")
        self.events.append("transaction:BEGIN")
        self.transaction_events.append("BEGIN")
        try:
            result = callback()
            if inspect.isawaitable(result):
                close = getattr(result, "close", None)
                if callable(close):
                    close()
                raise TypeError("transactionSync result must not be awaitable")
        except BaseException:
            self.connection.execute("ROLLBACK")
            self.events.append("transaction:ROLLBACK")
            self.transaction_events.append("ROLLBACK")
            raise

        self.connection.execute("COMMIT")
        self.events.append("transaction:COMMIT")
        self.transaction_events.append("COMMIT")
        return result

    async def setAlarm(self, scheduled_time_ms: int) -> None:
        self.events.append(f"alarm:set:{scheduled_time_ms}")
        if self.fail_next_set_alarm:
            self.fail_next_set_alarm = False
            raise RuntimeError("injected setAlarm failure")
        self.alarm_at_ms = scheduled_time_ms
        self.alarm_events.append(("setAlarm", scheduled_time_ms))

    async def getAlarm(self) -> int | None:
        self.events.append("alarm:get")
        self.alarm_events.append(("getAlarm", self.alarm_at_ms))
        return self.alarm_at_ms

    async def deleteAll(self) -> None:
        self.events.append("storage:deleteAll")
        if self.fail_next_delete_all:
            self.fail_next_delete_all = False
            raise RuntimeError("injected deleteAll failure")
        names = [
            cast(str, row[0])
            for row in self.connection.execute(
                "SELECT name FROM sqlite_master WHERE type = 'table' AND name NOT LIKE 'sqlite_%'"
            ).fetchall()
        ]
        self.connection.execute("BEGIN IMMEDIATE")
        try:
            for name in names:
                self.connection.execute(f'DROP TABLE "{name}"')
        except BaseException:
            self.connection.execute("ROLLBACK")
            raise
        self.connection.execute("COMMIT")
        self.delete_all_count += 1
        self.alarm_events.append(("deleteAll", None))

    def reset_observations(self) -> None:
        self.executions.clear()
        self.cursors.clear()
        self.events.clear()
        self.transaction_events.clear()

    def inject_sql_failure(self, statement: str, *, occurrence: int = 1) -> None:
        normalized_sql = normalize_sql(statement)
        if not normalized_sql:
            raise ValueError("statement must not be empty")
        if isinstance(occurrence, bool) or occurrence < 1:
            raise ValueError("occurrence must be a positive integer")
        self._sql_failures.append(
            _SqlFailure(normalized_sql=normalized_sql, occurrence=occurrence)
        )

    def consume_sql_failure(self, normalized_sql: str) -> bool:
        for failure in self._sql_failures:
            if failure.normalized_sql != normalized_sql:
                continue
            failure.seen += 1
            if failure.seen != failure.occurrence:
                continue
            self._sql_failures.remove(failure)
            return True
        return False

    @property
    def writes(self) -> int:
        return sum(
            execution.cursor.rowsWritten
            for execution in self.executions
            if execution.cursor is not None
        )

    @property
    def all_cursors_consumed(self) -> bool:
        return all(cursor.consumed for cursor in self.cursors)

    def database_snapshot(self) -> tuple[str, ...]:
        return tuple(self.connection.iterdump())

    def table_names(self) -> set[str]:
        return {
            cast(str, row[0])
            for row in self.connection.execute(
                "SELECT name FROM sqlite_master WHERE type = 'table' "
                + "AND name NOT LIKE 'sqlite_%'"
            ).fetchall()
        }

    def table_columns(self, table: str) -> tuple[str, ...]:
        self._require_table_name(table)
        return tuple(
            cast(str, row[1])
            for row in self.connection.execute(f'PRAGMA table_info("{table}")').fetchall()
        )

    def table_sql(self, table: str) -> str:
        self._require_table_name(table)
        row = self.connection.execute(
            "SELECT sql FROM sqlite_master WHERE type = 'table' AND name = ?",
            (table,),
        ).fetchone()
        if row is None or not isinstance(row[0], str):
            raise AssertionError(f"missing table: {table}")
        return row[0]

    def rows(self, table: str) -> list[dict[str, object]]:
        self._require_table_name(table)
        cursor = self.connection.execute(f'SELECT * FROM "{table}" ORDER BY rowid')
        names = tuple(column[0] for column in cursor.description or ())
        return [dict(zip(names, row, strict=True)) for row in cursor.fetchall()]

    def execute(self, statement: str, *bindings: object) -> FakeSqlCursor:
        cursor = self.sql.exec(statement, *bindings)
        list(cursor)
        return cursor

    @staticmethod
    def _require_table_name(table: str) -> None:
        if re.fullmatch(r"[a-z_]+", table) is None:
            raise ValueError("invalid test table name")


class FakeHeaders:
    """Small case-insensitive Fetch Headers stand-in."""

    def __init__(self, values: Mapping[str, str] | None = None) -> None:
        self._values = {key.casefold(): value for key, value in (values or {}).items()}

    def get(self, name: str) -> str | None:
        return self._values.get(name.casefold())


@dataclass(frozen=True, slots=True)
class FakeRequest:
    url: str
    method: str = "GET"
    headers: FakeHeaders = field(default_factory=FakeHeaders)


@dataclass(slots=True)
class _FakeWebSocketState:
    name: str
    events: list[str]
    js_id: int
    ready_state: int
    sent: list[str] = field(default_factory=list)
    close_calls: list[tuple[int, str]] = field(default_factory=list)
    fail_next_send: bool = False
    fail_next_close: bool = False
    fail_next_attachment: bool = False
    fail_next_serialize_attachment: bool = False
    fail_next_deserialize_attachment: bool = False
    attachment_json: str | None = None


class FakeWebSocket:
    """Synchronous hibernating socket fake with shared JavaScript state."""

    def __init__(
        self,
        name: str,
        events: list[str],
        *,
        ready_state: int = 1,
    ) -> None:
        state = _FakeWebSocketState(
            name=name,
            events=events,
            js_id=0,
            ready_state=ready_state,
        )
        state.js_id = id(state)
        self._state = state

    @classmethod
    def fresh_wrapper(cls, target: FakeWebSocket) -> FakeWebSocket:
        wrapper = cls.__new__(cls)
        wrapper._state = target._state
        return wrapper

    @property
    def name(self) -> str:
        return self._state.name

    @property
    def events(self) -> list[str]:
        return self._state.events

    @property
    def js_id(self) -> int:
        return self._state.js_id

    @property
    def readyState(self) -> int:
        return self._state.ready_state

    @readyState.setter
    def readyState(self, value: int) -> None:
        self._state.ready_state = value

    @property
    def sent(self) -> list[str]:
        return self._state.sent

    @property
    def close_calls(self) -> list[tuple[int, str]]:
        return self._state.close_calls

    @property
    def fail_next_send(self) -> bool:
        return self._state.fail_next_send

    @fail_next_send.setter
    def fail_next_send(self, value: bool) -> None:
        self._state.fail_next_send = value

    @property
    def fail_next_close(self) -> bool:
        return self._state.fail_next_close

    @fail_next_close.setter
    def fail_next_close(self, value: bool) -> None:
        self._state.fail_next_close = value

    @property
    def fail_next_attachment(self) -> bool:
        return self._state.fail_next_attachment

    @fail_next_attachment.setter
    def fail_next_attachment(self, value: bool) -> None:
        self._state.fail_next_attachment = value

    @property
    def fail_next_serialize_attachment(self) -> bool:
        return self._state.fail_next_serialize_attachment

    @fail_next_serialize_attachment.setter
    def fail_next_serialize_attachment(self, value: bool) -> None:
        self._state.fail_next_serialize_attachment = value

    @property
    def fail_next_deserialize_attachment(self) -> bool:
        return self._state.fail_next_deserialize_attachment

    @fail_next_deserialize_attachment.setter
    def fail_next_deserialize_attachment(self, value: bool) -> None:
        self._state.fail_next_deserialize_attachment = value

    @property
    def closed_with(self) -> tuple[int, str] | None:
        return self.close_calls[-1] if self.close_calls else None

    @property
    def attachment_json(self) -> str | None:
        return self._state.attachment_json

    def send(self, payload: object) -> None:
        self.events.append(f"socket:{self.name}:send")
        if self.fail_next_send:
            self.fail_next_send = False
            raise RuntimeError("injected send failure")
        if not isinstance(payload, str):
            raise TypeError("socket payload must be text")
        self.sent.append(payload)

    def close(self, code: int, reason: str) -> None:
        self.events.append(f"socket:{self.name}:close:{code}:{reason}")
        if self.fail_next_close:
            self.fail_next_close = False
            raise RuntimeError("injected close failure")
        self.close_calls.append((code, reason))
        self.readyState = 3

    def serializeAttachment(self, attachment: object) -> None:
        self.events.append(f"socket:{self.name}:attachment:serialize")
        if self.fail_next_attachment or self.fail_next_serialize_attachment:
            self.fail_next_attachment = False
            self.fail_next_serialize_attachment = False
            raise RuntimeError("injected attachment failure")
        if not isinstance(attachment, str):
            raise TypeError("socket attachment must be JSON text")
        decoded = json.loads(attachment)
        self._state.attachment_json = json.dumps(
            decoded,
            separators=(",", ":"),
            allow_nan=False,
        )

    def deserializeAttachment(self) -> object:
        self.events.append(f"socket:{self.name}:attachment:deserialize")
        if self.fail_next_attachment or self.fail_next_deserialize_attachment:
            self.fail_next_attachment = False
            self.fail_next_deserialize_attachment = False
            raise RuntimeError("injected attachment failure")
        if self.attachment_json is None:
            return None
        decoded = json.loads(self.attachment_json)
        return json.dumps(decoded, separators=(",", ":"), allow_nan=False)

    def seed_attachment(self, attachment: str) -> None:
        self.serializeAttachment(attachment)


@dataclass(frozen=True, slots=True)
class _CreatedWebSocketPair:
    client: FakeWebSocket
    server: FakeWebSocket

    def object_values(self) -> tuple[FakeWebSocket, FakeWebSocket]:
        return self.client, self.server


class FakeWebSocketPair:
    """Factory mirroring ``WebSocketPair.new().object_values()``."""

    def __init__(self, events: list[str]) -> None:
        self.events = events
        self.created: list[_CreatedWebSocketPair] = []
        self.fail_next_new = False
        self.fail_next_server_send = False
        self.fail_next_server_attachment = False
        self.fail_next_client_close = False
        self.fail_next_server_close = False

    def new(self) -> _CreatedWebSocketPair:
        if self.fail_next_new:
            self.fail_next_new = False
            raise RuntimeError("injected pair construction failure")
        index = len(self.created)
        pair = _CreatedWebSocketPair(
            client=FakeWebSocket(f"pair-{index}-client", self.events),
            server=FakeWebSocket(f"pair-{index}-server", self.events),
        )
        pair.server.fail_next_send = self.fail_next_server_send
        pair.server.fail_next_serialize_attachment = self.fail_next_server_attachment
        pair.client.fail_next_close = self.fail_next_client_close
        pair.server.fail_next_close = self.fail_next_server_close
        self.fail_next_server_send = False
        self.fail_next_server_attachment = False
        self.fail_next_client_close = False
        self.fail_next_server_close = False
        self.created.append(pair)
        self.events.append(f"pair:{index}:new")
        return pair


class FakeRoomContext:
    """Durable Object context sharing storage, sockets, and one event log."""

    def __init__(self, storage: FakeRoomStorage) -> None:
        self.storage = storage
        self.sockets: list[FakeWebSocket] = []
        self.fail_next_accept = False
        self.fresh_socket_wrappers = False
        self.block_concurrency_callbacks: list[Callable[[], Awaitable[object]]] = []

    def blockConcurrencyWhile(
        self,
        callback: Callable[[], Awaitable[object]],
    ) -> None:
        if not inspect.iscoroutinefunction(callback):
            raise TypeError("blockConcurrencyWhile requires an async callable")
        self.block_concurrency_callbacks.append(callback)
        coroutine = callback()
        try:
            coroutine.send(None)
        except StopIteration:
            return
        coroutine.close()
        raise RuntimeError("fake blockConcurrencyWhile callback unexpectedly suspended")

    def acceptWebSocket(self, socket: object) -> None:
        checked_socket = cast(FakeWebSocket, socket)
        self.storage.events.append(f"socket:{checked_socket.name}:accept")
        if self.fail_next_accept:
            self.fail_next_accept = False
            raise RuntimeError("injected accept failure")
        if all(socket.js_id != checked_socket.js_id for socket in self.sockets):
            self.sockets.append(checked_socket)

    def getWebSockets(self) -> list[FakeWebSocket]:
        if self.fresh_socket_wrappers:
            return [FakeWebSocket.fresh_wrapper(socket) for socket in self.sockets]
        return list(self.sockets)

    def add_socket(self, socket: FakeWebSocket) -> None:
        if all(existing.js_id != socket.js_id for existing in self.sockets):
            self.sockets.append(socket)


__all__ = [
    "FakeHeaders",
    "FakeRequest",
    "FakeRoomContext",
    "FakeRoomSql",
    "FakeRoomStorage",
    "FakeSqlCursor",
    "FakeWebSocket",
    "FakeWebSocketPair",
    "SqlExecution",
]
