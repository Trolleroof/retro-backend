"""SQLite persistence and atomic lifecycle transitions for one 2048 room."""

from __future__ import annotations

import json
import re
import secrets
from collections.abc import Callable, Collection, Mapping
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Literal, cast

from .bindings import RoomStorage, SqlRow
from .errors import RoomFault, StoredStateFault
from .games.game_2048 import (
    Game2048Adapter,
    Game2048State,
    MoveError,
    MoveNoop,
)
from .protocol import (
    MAX_SAFE_INTEGER,
    ROOM_ID_PATTERN,
    Game2048Snapshot,
    GameDescriptor,
    MoveCommand,
    ReadyCommand,
    RoomStatus,
)
from .tickets import (
    TICKET_TTL_MS,
    EntropySource,
    generate_ticket,
    hash_ticket,
    validate_ticket,
)

WAITING_ROOM_TTL_MS = 30 * 60 * 1_000
ACTIVE_ROOM_TTL_MS = 30 * 60 * 1_000
COMPLETED_ROOM_TTL_MS = 24 * 60 * 60 * 1_000

# Last whole UTC second representable by Python's aware datetime and JavaScript-safe.
MAX_TIMESTAMP_MS = 253_402_300_799_000

ROOM_SCHEMA: tuple[str, ...] = (
    """CREATE TABLE IF NOT EXISTS room (
        singleton INTEGER PRIMARY KEY CHECK (singleton = 1),
        room_id TEXT NOT NULL UNIQUE,
        game_type TEXT NOT NULL,
        mode TEXT NOT NULL,
        status TEXT NOT NULL CHECK (status IN ('waiting', 'active', 'completed', 'expired')),
        created_by TEXT NOT NULL,
        created_at INTEGER NOT NULL,
        expires_at INTEGER NOT NULL,
        version INTEGER NOT NULL CHECK (version BETWEEN 0 AND 9007199254740991),
        state_json TEXT NOT NULL,
        CHECK (game_type = '2048' AND mode = '1v1')
    )""",
    """CREATE TABLE IF NOT EXISTS participants (
        user_id TEXT PRIMARY KEY,
        seat INTEGER NOT NULL UNIQUE CHECK (seat BETWEEN 0 AND 1),
        ready INTEGER NOT NULL DEFAULT 0 CHECK (ready IN (0, 1)),
        joined_at INTEGER NOT NULL,
        disconnected_at INTEGER
    )""",
    """CREATE TABLE IF NOT EXISTS tickets (
        token_hash TEXT PRIMARY KEY,
        user_id TEXT NOT NULL,
        issued_at INTEGER NOT NULL,
        expires_at INTEGER NOT NULL,
        consumed_at INTEGER
    )""",
    """CREATE TABLE IF NOT EXISTS actions (
        sequence INTEGER PRIMARY KEY AUTOINCREMENT,
        user_id TEXT NOT NULL,
        seat INTEGER NOT NULL CHECK (seat BETWEEN 0 AND 1),
        action_type TEXT NOT NULL CHECK (action_type IN ('ready', 'move')),
        action_json TEXT NOT NULL,
        accepted_at INTEGER NOT NULL,
        resulting_version INTEGER NOT NULL UNIQUE
    )""",
)

_ROOM_ID_RE = re.compile(ROOM_ID_PATTERN)
_EPOCH = datetime(1970, 1, 1, tzinfo=UTC)
_STORAGE_ERROR_MESSAGE = "Room storage operation failed"

type Audience = Literal["actor", "all"]
type ExpirationKind = Literal["none", "reschedule", "expired"]


class StorageFault(RuntimeError):
    """An internal SQL or private-state failure safe to collapse at the RPC edge."""

    def __init__(self) -> None:
        super().__init__(_STORAGE_ERROR_MESSAGE)


class _SocketSetupFailure(RuntimeError):
    """A non-storage failure raised by a socket setup callback."""


@dataclass(frozen=True, slots=True)
class SocketIdentity:
    user_id: str
    seat: Literal[0, 1]


@dataclass(frozen=True, slots=True)
class CreateRoomResult:
    snapshot: Game2048Snapshot
    created: bool


@dataclass(frozen=True, slots=True)
class IssuedTicket:
    ticket: str
    expires_at: datetime


@dataclass(frozen=True, slots=True)
class CommandResult:
    snapshot: Game2048Snapshot
    audience: Audience
    changed: bool
    next_alarm: int | None


@dataclass(frozen=True, slots=True)
class ExpirationResult:
    kind: ExpirationKind
    snapshot: Game2048Snapshot | None
    next_alarm: int | None


@dataclass(frozen=True, slots=True)
class _SqlResult:
    rows: tuple[SqlRow, ...]


@dataclass(frozen=True, slots=True)
class _RoomRecord:
    room_id: str
    game_type: Literal["2048"]
    mode: Literal["1v1"]
    status: RoomStatus
    created_by: str
    created_at: int
    expires_at: int
    version: int
    state_json: str
    state: Game2048State


@dataclass(frozen=True, slots=True)
class _ParticipantRecord:
    user_id: str
    seat: Literal[0, 1]
    ready: bool
    joined_at: int
    disconnected_at: int | None


@dataclass(frozen=True, slots=True)
class _TicketRecord:
    user_id: str
    issued_at: int
    expires_at: int
    consumed_at: int | None


class RoomStore:
    """Own one room database and keep every gameplay mutation synchronous."""

    def __init__(
        self,
        storage: RoomStorage,
        *,
        entropy: EntropySource = secrets.token_bytes,
        random_uint32: Callable[[], int] | None = None,
        adapter: Game2048Adapter | None = None,
    ) -> None:
        self._storage = storage
        self._entropy = entropy
        self._random_uint32 = random_uint32 or (lambda: secrets.randbits(32))
        self._adapter = adapter or Game2048Adapter()

    def initialize_schema(self) -> None:
        try:
            for statement in ROOM_SCHEMA:
                self._execute(statement)
        except StorageFault:
            raise
        except Exception as error:
            raise StorageFault from error

    def retained_alarm_deadline(self) -> int | None:
        room = self._read_optional_room()
        if room is None or room.status == "expired":
            return None
        return room.expires_at

    def create_room(
        self,
        *,
        room_id: object,
        descriptor: object,
        created_by: object,
        now_ms: object,
        connected_user_ids: Collection[str] = (),
    ) -> Game2048Snapshot:
        return self.create_room_with_status(
            room_id=room_id,
            descriptor=descriptor,
            created_by=created_by,
            now_ms=now_ms,
            connected_user_ids=connected_user_ids,
        ).snapshot

    def create_room_with_status(
        self,
        *,
        room_id: object,
        descriptor: object,
        created_by: object,
        now_ms: object,
        connected_user_ids: Collection[str] = (),
    ) -> CreateRoomResult:
        checked_room_id = _validate_room_id(room_id)
        checked_creator = _validate_identifier(created_by)
        checked_descriptor = self._validate_descriptor(descriptor)
        checked_now = _validate_timestamp(now_ms)
        expires_at = _checked_deadline(checked_now, WAITING_ROOM_TTL_MS)
        connected = _validate_connected_users(connected_user_ids)

        def transition() -> CreateRoomResult:
            existing = self._read_optional_room()
            if existing is not None:
                participants = self._read_validated_participants(existing)
                if (
                    existing.room_id != checked_room_id
                    or existing.game_type != checked_descriptor.game_type
                    or existing.mode != checked_descriptor.mode
                    or existing.created_by != checked_creator
                ):
                    raise RoomFault.for_code("invalid_request")
                return CreateRoomResult(
                    snapshot=self._snapshot(existing, participants, connected),
                    created=False,
                )

            if self._read_participants():
                raise StorageFault

            waiting_state = self._adapter.waiting_state()
            self._decode_adapter_state(waiting_state, "waiting")
            state_json = _serialize_json(waiting_state.to_payload())
            inserted_room = self._execute(
                """INSERT INTO room (
                    singleton, room_id, game_type, mode, status, created_by,
                    created_at, expires_at, version, state_json
                ) VALUES (1, ?, ?, ?, 'waiting', ?, ?, ?, 0, ?)
                RETURNING singleton""",
                checked_room_id,
                checked_descriptor.game_type,
                checked_descriptor.mode,
                checked_creator,
                checked_now,
                expires_at,
                state_json,
            )
            _require_returned_value(inserted_room, "singleton", 1)
            inserted_participant = self._execute(
                """INSERT INTO participants (
                    user_id, seat, ready, joined_at, disconnected_at
                ) VALUES (?, 0, 0, ?, NULL)
                RETURNING seat""",
                checked_creator,
                checked_now,
            )
            _require_returned_value(inserted_participant, "seat", 0)

            room = self._require_room()
            return CreateRoomResult(
                snapshot=self._snapshot(room, self._read_participants(), connected),
                created=True,
            )

        return self._transaction(transition)

    def get_snapshot(
        self,
        user_id: object,
        *,
        connected_user_ids: Collection[str] = (),
    ) -> Game2048Snapshot:
        checked_user_id = _validate_identifier(user_id)
        connected = _validate_connected_users(connected_user_ids)
        try:
            room = self._require_room()
            participants = self._read_validated_participants(room)
            if not any(item.user_id == checked_user_id for item in participants):
                raise RoomFault.for_code("not_joined")
            return self._snapshot(room, participants, connected)
        except (RoomFault, StorageFault):
            raise
        except Exception as error:
            raise StorageFault from error

    def require_room_id(self, room_id: object) -> None:
        """Verify this object's persisted room identity without mutating it."""
        checked_room_id = _validate_room_id(room_id)
        try:
            room = self._require_room()
            self._read_validated_participants(room)
            if room.room_id != checked_room_id:
                raise RoomFault.for_code("room_not_found")
        except (RoomFault, StorageFault):
            raise
        except Exception as error:
            raise StorageFault from error

    def join(
        self,
        user_id: object,
        now_ms: object,
        *,
        connected_user_ids: Collection[str] = (),
    ) -> Game2048Snapshot:
        checked_user_id = _validate_identifier(user_id)
        checked_now = _validate_timestamp(now_ms)
        connected = _validate_connected_users(connected_user_ids)

        def transition() -> Game2048Snapshot:
            room = self._require_room()
            participants = self._read_validated_participants(room)
            _require_wall_clock_activity(room, checked_now)
            existing = next(
                (item for item in participants if item.user_id == checked_user_id),
                None,
            )
            if existing is not None:
                return self._snapshot(room, participants, connected)

            occupied = {participant.seat for participant in participants}
            seat = next((candidate for candidate in (0, 1) if candidate not in occupied), None)
            if seat is None:
                raise RoomFault.for_code("room_full")
            inserted = self._execute(
                """INSERT INTO participants (
                    user_id, seat, ready, joined_at, disconnected_at
                ) VALUES (?, ?, 0, ?, NULL)
                RETURNING seat""",
                checked_user_id,
                seat,
                checked_now,
            )
            _require_returned_value(inserted, "seat", seat)
            final_room = self._require_room()
            return self._snapshot(final_room, self._read_participants(), connected)

        return self._transaction(transition)

    def issue_ticket(self, user_id: object, now_ms: object) -> IssuedTicket:
        checked_user_id = _validate_identifier(user_id)
        checked_now = _validate_timestamp(now_ms)
        expires_at = _checked_deadline(checked_now, TICKET_TTL_MS)

        def transition() -> IssuedTicket:
            room = self._require_room()
            participants = self._read_validated_participants(room)
            _require_wall_clock_activity(room, checked_now)
            if not any(item.user_id == checked_user_id for item in participants):
                raise RoomFault.for_code("not_joined")

            self._execute("DELETE FROM tickets WHERE expires_at <= ?", checked_now)
            ticket = generate_ticket(self._entropy)
            token_hash = hash_ticket(ticket)
            inserted = self._execute(
                """INSERT INTO tickets (
                    token_hash, user_id, issued_at, expires_at, consumed_at
                ) VALUES (?, ?, ?, ?, NULL)
                RETURNING token_hash""",
                token_hash,
                checked_user_id,
                checked_now,
                expires_at,
            )
            _require_returned_value(inserted, "token_hash", token_hash)
            return IssuedTicket(ticket=ticket, expires_at=_timestamp_to_datetime(expires_at))

        return self._transaction(transition)

    def consume_ticket(self, raw_ticket: object, now_ms: object) -> SocketIdentity:
        checked_now = _validate_timestamp(now_ms)
        checked_ticket = validate_ticket(raw_ticket)
        token_hash = hash_ticket(checked_ticket)

        def transition() -> SocketIdentity:
            identity = self._inspect_ticket(token_hash, checked_now)
            self._mark_ticket_consumed(token_hash, checked_now)
            return identity

        return self._transaction(transition)

    def setup_socket[SetupT, ResultT](
        self,
        raw_ticket: object,
        now_ms: object,
        *,
        create: Callable[[SocketIdentity], SetupT],
        accept: Callable[[SetupT, SocketIdentity], None],
        finish: Callable[[SetupT, SocketIdentity], ResultT],
    ) -> ResultT:
        """Run storage mutation and synchronous socket setup in one transaction."""
        checked_now = _validate_timestamp(now_ms)
        checked_ticket = validate_ticket(raw_ticket)
        token_hash = hash_ticket(checked_ticket)

        def setup_step[ValueT](callback: Callable[[], ValueT]) -> ValueT:
            try:
                return callback()
            except (RoomFault, StorageFault):
                raise
            except Exception as error:
                raise _SocketSetupFailure from error

        def transition() -> ResultT:
            identity = self._inspect_ticket(token_hash, checked_now)
            state = setup_step(lambda: create(identity))
            self._mark_ticket_consumed(token_hash, checked_now)
            setup_step(lambda: accept(state, identity))
            self._mark_connected(identity)
            return setup_step(lambda: finish(state, identity))

        try:
            return self._storage.transactionSync(transition)
        except (RoomFault, StorageFault, _SocketSetupFailure):
            raise
        except Exception as error:
            raise StorageFault from error

    def mark_connected(self, identity: object) -> None:
        checked_identity = _validate_identity(identity)

        def transition() -> None:
            self._mark_connected(checked_identity)

        self._transaction(transition)

    def _inspect_ticket(self, token_hash: str, now_ms: int) -> SocketIdentity:
        room = self._require_room()
        participants = self._read_validated_participants(room)
        _require_wall_clock_activity(room, now_ms)
        result = self._execute(
            """SELECT user_id, issued_at, expires_at, consumed_at
            FROM tickets WHERE token_hash = ?""",
            token_hash,
        )
        if not result.rows:
            raise RoomFault.for_code("invalid_ticket")
        if len(result.rows) != 1:
            raise StorageFault
        ticket = _parse_ticket(result.rows[0], now_ms)
        if ticket.consumed_at is not None:
            raise RoomFault.for_code("ticket_consumed")
        if now_ms >= ticket.expires_at:
            raise RoomFault.for_code("ticket_expired")

        participant = next(
            (item for item in participants if item.user_id == ticket.user_id),
            None,
        )
        if participant is None:
            raise RoomFault.for_code("invalid_ticket")
        return SocketIdentity(participant.user_id, participant.seat)

    def _mark_ticket_consumed(self, token_hash: str, now_ms: int) -> None:
        updated = self._execute(
            """UPDATE tickets SET consumed_at = ?
            WHERE token_hash = ? AND consumed_at IS NULL
            RETURNING token_hash""",
            now_ms,
            token_hash,
        )
        if not updated.rows:
            raise RoomFault.for_code("ticket_consumed")
        _require_returned_value(updated, "token_hash", token_hash)

    def _mark_connected(self, identity: SocketIdentity) -> None:
        room = self._require_room()
        participants = self._read_validated_participants(room)
        participant = self._require_identity_participant(identity, participants)
        updated = self._execute(
            """UPDATE participants SET disconnected_at = NULL
            WHERE user_id = ? AND seat = ?
            RETURNING user_id""",
            participant.user_id,
            participant.seat,
        )
        _require_returned_value(updated, "user_id", participant.user_id)

    def mark_disconnected(self, identity: object, now_ms: object) -> None:
        checked_identity = _validate_identity(identity)
        checked_now = _validate_timestamp(now_ms)

        def transition() -> None:
            room = self._require_room()
            participants = self._read_validated_participants(room)
            participant = self._require_identity_participant(
                checked_identity,
                participants,
            )
            if checked_now < participant.joined_at:
                raise StorageFault
            updated = self._execute(
                """UPDATE participants SET disconnected_at = ?
                WHERE user_id = ? AND seat = ?
                RETURNING user_id""",
                checked_now,
                participant.user_id,
                participant.seat,
            )
            _require_returned_value(updated, "user_id", participant.user_id)

        self._transaction(transition)

    def execute_command(
        self,
        identity: object,
        command: object,
        now_ms: object,
        *,
        connected_user_ids: Collection[str] = (),
    ) -> CommandResult:
        checked_identity = _validate_identity(identity)
        checked_now = _validate_timestamp(now_ms)
        connected = _validate_connected_users(connected_user_ids)
        if not isinstance(command, (ReadyCommand, MoveCommand)):
            raise RoomFault.for_code("invalid_request")

        def transition() -> CommandResult:
            room = self._require_room()
            participants = self._read_validated_participants(room)
            _require_wall_clock_activity(room, checked_now)
            participant = self._require_identity_participant(
                checked_identity,
                participants,
            )
            if isinstance(command, ReadyCommand):
                return self._execute_ready(
                    room,
                    participant,
                    command,
                    checked_now,
                    connected,
                )
            return self._execute_move(
                room,
                participant,
                command,
                checked_now,
                connected,
            )

        return self._transaction(transition)

    def expire(
        self,
        now_ms: object,
        *,
        connected_user_ids: Collection[str] = (),
    ) -> ExpirationResult:
        checked_now = _validate_timestamp(now_ms)
        connected = _validate_connected_users(connected_user_ids)

        def transition() -> ExpirationResult:
            room = self._read_optional_room()
            if room is None:
                if self._read_participants():
                    raise StorageFault
                return ExpirationResult(kind="none", snapshot=None, next_alarm=None)
            participants = self._read_validated_participants(room)
            if room.status == "expired":
                return ExpirationResult(
                    kind="expired",
                    snapshot=self._snapshot(room, participants, connected),
                    next_alarm=None,
                )
            if checked_now < room.expires_at:
                return ExpirationResult(
                    kind="reschedule",
                    snapshot=None,
                    next_alarm=room.expires_at,
                )

            next_version = _next_version(room.version)
            try:
                expired_state = self._adapter.expire(room.state)
            except Exception as error:
                raise StorageFault from error
            self._decode_adapter_state(expired_state, "expired")
            state_json = _serialize_json(expired_state.to_payload())
            updated = self._execute(
                """UPDATE room SET status = 'expired', version = ?, state_json = ?
                WHERE singleton = 1 AND status != 'expired' AND version = ?
                RETURNING version""",
                next_version,
                state_json,
                room.version,
            )
            _require_returned_value(updated, "version", next_version)
            self._execute("DELETE FROM tickets")
            final_room = self._require_room()
            final_snapshot = self._snapshot(
                final_room,
                self._read_participants(),
                connected,
            )
            return ExpirationResult(
                kind="expired",
                snapshot=final_snapshot,
                next_alarm=None,
            )

        return self._transaction(transition)

    def _execute_ready(
        self,
        room: _RoomRecord,
        participant: _ParticipantRecord,
        command: ReadyCommand,
        now_ms: int,
        connected: frozenset[str],
    ) -> CommandResult:
        if room.status != "waiting":
            raise RoomFault.for_code("match_not_active")
        if command.expected_version != room.version:
            raise RoomFault.for_code(
                "stale_version",
                details={"current_version": room.version},
            )
        if participant.ready:
            return CommandResult(
                snapshot=self._snapshot(room, self._read_participants(), connected),
                audience="all",
                changed=False,
                next_alarm=None,
            )

        next_version = _next_version(room.version)
        updated_participant = self._execute(
            """UPDATE participants SET ready = 1
            WHERE user_id = ? AND seat = ? AND ready = 0
            RETURNING user_id""",
            participant.user_id,
            participant.seat,
        )
        _require_returned_value(updated_participant, "user_id", participant.user_id)
        participants = self._read_participants()
        activates = len(participants) == 2 and all(item.ready for item in participants)

        next_status: RoomStatus = "active" if activates else "waiting"
        next_state = room.state
        next_expires_at = room.expires_at
        next_alarm: int | None = None
        if activates:
            try:
                next_state = self._adapter.start(room.state, self._random_uint32)
            except Exception as error:
                raise StorageFault from error
            self._decode_adapter_state(next_state, "active")
            next_expires_at = _checked_deadline(now_ms, ACTIVE_ROOM_TTL_MS)
            next_alarm = next_expires_at

        state_json = _serialize_json(next_state.to_payload())
        updated_room = self._execute(
            """UPDATE room
            SET version = ?, status = ?, state_json = ?, expires_at = ?
            WHERE singleton = 1 AND version = ?
            RETURNING version""",
            next_version,
            next_status,
            state_json,
            next_expires_at,
            room.version,
        )
        _require_returned_value(updated_room, "version", next_version)
        self._insert_action(participant, "ready", {}, now_ms, next_version)
        final_room = self._require_room()
        return CommandResult(
            snapshot=self._snapshot(final_room, self._read_participants(), connected),
            audience="all",
            changed=True,
            next_alarm=next_alarm,
        )

    def _execute_move(
        self,
        room: _RoomRecord,
        participant: _ParticipantRecord,
        command: MoveCommand,
        now_ms: int,
        connected: frozenset[str],
    ) -> CommandResult:
        if room.status != "active":
            raise RoomFault.for_code("match_not_active")
        try:
            applied = self._adapter.apply_command(room.state, participant.seat, command)
        except StoredStateFault as error:
            raise StorageFault from error
        except Exception as error:
            raise StorageFault from error

        if isinstance(applied, MoveError):
            if applied.code == "stale_board_version":
                raise RoomFault.for_code(
                    "stale_board_version",
                    details={
                        "current_version": room.version,
                        "current_board_version": applied.current_board_version,
                    },
                )
            raise RoomFault.for_code("match_not_active")
        if isinstance(applied, MoveNoop):
            return CommandResult(
                snapshot=self._snapshot(room, self._read_participants(), connected),
                audience="actor",
                changed=False,
                next_alarm=None,
            )
        next_version = _next_version(room.version)
        next_status: RoomStatus = "completed" if applied.completed else "active"
        next_expires_at = room.expires_at
        next_alarm: int | None = None
        if applied.completed:
            next_expires_at = _checked_deadline(now_ms, COMPLETED_ROOM_TTL_MS)
            next_alarm = next_expires_at
        self._decode_adapter_state(applied.state, next_status)
        state_json = _serialize_json(applied.state.to_payload())
        updated_room = self._execute(
            """UPDATE room
            SET state_json = ?, status = ?, version = ?, expires_at = ?
            WHERE singleton = 1 AND version = ?
            RETURNING version""",
            state_json,
            next_status,
            next_version,
            next_expires_at,
            room.version,
        )
        _require_returned_value(updated_room, "version", next_version)
        self._insert_action(
            participant,
            "move",
            applied.action_payload,
            now_ms,
            next_version,
        )
        final_room = self._require_room()
        return CommandResult(
            snapshot=self._snapshot(final_room, self._read_participants(), connected),
            audience="all",
            changed=True,
            next_alarm=next_alarm,
        )

    def _insert_action(
        self,
        participant: _ParticipantRecord,
        action_type: Literal["ready", "move"],
        payload: Mapping[str, object],
        accepted_at: int,
        resulting_version: int,
    ) -> None:
        inserted = self._execute(
            """INSERT INTO actions (
                user_id, seat, action_type, action_json, accepted_at, resulting_version
            ) VALUES (?, ?, ?, ?, ?, ?)
            RETURNING resulting_version""",
            participant.user_id,
            participant.seat,
            action_type,
            _serialize_json(payload),
            accepted_at,
            resulting_version,
        )
        _require_returned_value(inserted, "resulting_version", resulting_version)

    def _validate_descriptor(self, descriptor: object) -> GameDescriptor:
        if not isinstance(descriptor, GameDescriptor) or descriptor != self._adapter.descriptor:
            raise RoomFault.for_code("invalid_request")
        return descriptor

    def _transaction[ResultT](self, callback: Callable[[], ResultT]) -> ResultT:
        try:
            return self._storage.transactionSync(callback)
        except (RoomFault, StorageFault):
            raise
        except Exception as error:
            raise StorageFault from error

    def _execute(self, statement: str, *bindings: object) -> _SqlResult:
        cursor = self._storage.sql.exec(statement, *bindings)
        rows = tuple(cursor)
        return _SqlResult(rows=rows)

    def _read_optional_room(self) -> _RoomRecord | None:
        result = self._execute(
            """SELECT room_id, game_type, mode, status, created_by,
            created_at, expires_at, version, state_json
            FROM room WHERE singleton = 1"""
        )
        if not result.rows:
            return None
        if len(result.rows) != 1:
            raise StorageFault
        row = result.rows[0]
        game_type = _field(row, "game_type")
        mode = _field(row, "mode")
        if game_type != "2048" or type(game_type) is not str:
            raise StorageFault
        if mode != "1v1" or type(mode) is not str:
            raise StorageFault
        status = _stored_status(_field(row, "status"))
        state_json = _field(row, "state_json")
        if type(state_json) is not str:
            raise StorageFault
        state = self._decode_json_state(state_json, status)
        created_at = _stored_timestamp(_field(row, "created_at"))
        expires_at = _stored_timestamp(_field(row, "expires_at"))
        if expires_at < created_at:
            raise StorageFault
        return _RoomRecord(
            room_id=_stored_room_id(_field(row, "room_id")),
            game_type="2048",
            mode="1v1",
            status=status,
            created_by=_stored_identifier(_field(row, "created_by")),
            created_at=created_at,
            expires_at=expires_at,
            version=_stored_safe_integer(_field(row, "version")),
            state_json=state_json,
            state=state,
        )

    def _require_room(self) -> _RoomRecord:
        room = self._read_optional_room()
        if room is None:
            raise RoomFault.for_code("room_not_found")
        return room

    def _read_participants(self) -> tuple[_ParticipantRecord, ...]:
        result = self._execute(
            """SELECT user_id, seat, ready, joined_at, disconnected_at
            FROM participants ORDER BY seat"""
        )
        participants = tuple(_parse_participant(row) for row in result.rows)
        if len(participants) > 2 or len({item.seat for item in participants}) != len(
            participants
        ):
            raise StorageFault
        return participants

    def _read_validated_participants(
        self,
        room: _RoomRecord,
    ) -> tuple[_ParticipantRecord, ...]:
        participants = self._read_participants()
        self._validate_topology(room, participants)
        return participants

    @staticmethod
    def _validate_topology(
        room: _RoomRecord,
        participants: tuple[_ParticipantRecord, ...],
    ) -> None:
        if not 1 <= len(participants) <= 2:
            raise StorageFault
        if len({item.user_id for item in participants}) != len(participants):
            raise StorageFault
        creator = next(
            (item for item in participants if item.user_id == room.created_by),
            None,
        )
        if creator is None or creator.seat != 0:
            raise StorageFault

        seats = {item.seat for item in participants}
        requires_full_ready_topology = room.status in {"active", "completed"} or (
            room.status == "expired" and room.state.boards is not None
        )
        if requires_full_ready_topology:
            if seats != {0, 1} or not all(item.ready for item in participants):
                raise StorageFault
            return
        if seats not in ({0}, {0, 1}):
            raise StorageFault

    def _require_identity_participant(
        self,
        identity: SocketIdentity,
        participants: tuple[_ParticipantRecord, ...],
    ) -> _ParticipantRecord:
        participant = next(
            (item for item in participants if item.user_id == identity.user_id),
            None,
        )
        if participant is None or participant.seat != identity.seat:
            raise RoomFault.for_code("not_joined")
        return participant

    def _decode_json_state(self, state_json: str, status: RoomStatus) -> Game2048State:
        try:
            value = json.loads(state_json, parse_constant=_reject_json_constant)
        except (TypeError, ValueError) as error:
            raise StorageFault from error
        return self._decode_adapter_state(value, status)

    def _decode_adapter_state(self, state: object, status: RoomStatus) -> Game2048State:
        try:
            return self._adapter.decode_state(state, status)
        except StoredStateFault as error:
            raise StorageFault from error
        except Exception as error:
            raise StorageFault from error

    def _snapshot(
        self,
        room: _RoomRecord,
        participants: tuple[_ParticipantRecord, ...],
        connected: frozenset[str],
    ) -> Game2048Snapshot:
        try:
            self._validate_topology(room, participants)
            public_state = self._adapter.public_state(room.state, room.status)
            payload: dict[str, object] = {
                "room_id": room.room_id,
                "game_type": room.game_type,
                "mode": room.mode,
                "status": room.status,
                "created_at": _timestamp_to_datetime(room.created_at),
                "expires_at": _timestamp_to_datetime(room.expires_at),
                "version": room.version,
                "participants": tuple(
                    {
                        "user_id": participant.user_id,
                        "seat": participant.seat,
                        "ready": participant.ready,
                        "connected": participant.user_id in connected,
                    }
                    for participant in participants
                ),
                **public_state,
            }
            return Game2048Snapshot.model_validate(payload)
        except StorageFault:
            raise
        except Exception as error:
            raise StorageFault from error


def _field(row: SqlRow, name: str) -> object:
    if name not in row:
        raise StorageFault
    return row[name]


def _parse_participant(row: SqlRow) -> _ParticipantRecord:
    seat_value = _field(row, "seat")
    if type(seat_value) is not int or seat_value not in {0, 1}:
        raise StorageFault
    ready_value = _field(row, "ready")
    if type(ready_value) is not int or ready_value not in {0, 1}:
        raise StorageFault
    joined_at = _stored_timestamp(_field(row, "joined_at"))
    disconnected_at = _stored_optional_timestamp(_field(row, "disconnected_at"))
    if disconnected_at is not None and disconnected_at < joined_at:
        raise StorageFault
    return _ParticipantRecord(
        user_id=_stored_identifier(_field(row, "user_id")),
        seat=cast(Literal[0, 1], seat_value),
        ready=ready_value == 1,
        joined_at=joined_at,
        disconnected_at=disconnected_at,
    )


def _parse_ticket(row: SqlRow, now_ms: int) -> _TicketRecord:
    issued_at = _stored_timestamp(_field(row, "issued_at"))
    expires_at = _stored_timestamp(_field(row, "expires_at"))
    consumed_at = _stored_optional_timestamp(_field(row, "consumed_at"))
    if issued_at > MAX_TIMESTAMP_MS - TICKET_TTL_MS:
        raise StorageFault
    if expires_at != issued_at + TICKET_TTL_MS or issued_at > now_ms:
        raise StorageFault
    if consumed_at is not None and (
        not issued_at <= consumed_at < expires_at or consumed_at > now_ms
    ):
        raise StorageFault
    return _TicketRecord(
        user_id=_stored_identifier(_field(row, "user_id")),
        issued_at=issued_at,
        expires_at=expires_at,
        consumed_at=consumed_at,
    )


def _serialize_json(value: object) -> str:
    try:
        return json.dumps(value, separators=(",", ":"), allow_nan=False)
    except (TypeError, ValueError) as error:
        raise StorageFault from error


def _reject_json_constant(_constant: str) -> None:
    raise ValueError("non-standard JSON constant")


def _validate_room_id(value: object) -> str:
    if type(value) is not str or _ROOM_ID_RE.fullmatch(value) is None:
        raise RoomFault.for_code("invalid_request")
    return value


def _validate_identifier(value: object) -> str:
    if type(value) is not str or not value:
        raise RoomFault.for_code("invalid_request")
    return value


def _validate_identity(value: object) -> SocketIdentity:
    if not isinstance(value, SocketIdentity):
        raise RoomFault.for_code("not_joined")
    user_id = _validate_identifier(value.user_id)
    if type(value.seat) is not int or value.seat not in {0, 1}:
        raise RoomFault.for_code("not_joined")
    return SocketIdentity(user_id, cast(Literal[0, 1], value.seat))


def _validate_connected_users(values: Collection[str]) -> frozenset[str]:
    try:
        return frozenset(_validate_identifier(value) for value in values)
    except TypeError as error:
        raise RoomFault.for_code("invalid_request") from error


def _validate_timestamp(value: object) -> int:
    if type(value) is not int or not 0 <= value <= MAX_TIMESTAMP_MS:
        raise RoomFault.for_code("invalid_request")
    return value


def _checked_deadline(now_ms: int, duration_ms: int) -> int:
    if type(duration_ms) is not int or duration_ms < 0 or now_ms > MAX_TIMESTAMP_MS - duration_ms:
        raise RoomFault.for_code("invalid_request")
    return now_ms + duration_ms


def _timestamp_to_datetime(value: int) -> datetime:
    try:
        return _EPOCH + timedelta(milliseconds=value)
    except (OverflowError, ValueError) as error:
        raise StorageFault from error


def _stored_timestamp(value: object) -> int:
    if type(value) is not int or not 0 <= value <= MAX_TIMESTAMP_MS:
        raise StorageFault
    return value


def _stored_optional_timestamp(value: object) -> int | None:
    if value is None:
        return None
    return _stored_timestamp(value)


def _stored_safe_integer(value: object) -> int:
    if type(value) is not int or not 0 <= value <= MAX_SAFE_INTEGER:
        raise StorageFault
    return value


def _stored_identifier(value: object) -> str:
    if type(value) is not str or not value:
        raise StorageFault
    return value


def _stored_room_id(value: object) -> str:
    if type(value) is not str or _ROOM_ID_RE.fullmatch(value) is None:
        raise StorageFault
    return value


def _stored_status(value: object) -> RoomStatus:
    if type(value) is not str or value not in {"waiting", "active", "completed", "expired"}:
        raise StorageFault
    return cast(RoomStatus, value)


def _next_version(current: int) -> int:
    if type(current) is not int or not 0 <= current < MAX_SAFE_INTEGER:
        raise StorageFault
    return current + 1


def _require_wall_clock_activity(room: _RoomRecord, now_ms: int) -> None:
    if room.status == "expired" or now_ms >= room.expires_at:
        raise RoomFault.for_code("room_expired")


def _require_returned_value(
    result: _SqlResult,
    field: str,
    expected: object,
) -> None:
    if len(result.rows) != 1:
        raise StorageFault
    returned = _field(result.rows[0], field)
    if type(returned) is not type(expected) or returned != expected:
        raise StorageFault


__all__ = [
    "ACTIVE_ROOM_TTL_MS",
    "COMPLETED_ROOM_TTL_MS",
    "MAX_TIMESTAMP_MS",
    "ROOM_SCHEMA",
    "WAITING_ROOM_TTL_MS",
    "CommandResult",
    "CreateRoomResult",
    "ExpirationResult",
    "IssuedTicket",
    "RoomStore",
    "SocketIdentity",
    "StorageFault",
]
