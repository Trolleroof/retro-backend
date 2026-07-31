"""Durable Object RPC, hibernating sockets, and alarms for one 2048 room."""

from __future__ import annotations

import json
import sys
import time
from collections.abc import Callable, Collection, Mapping
from contextlib import suppress
from datetime import UTC, datetime
from typing import Literal
from urllib.parse import parse_qsl, urlsplit

from pydantic import (
    AwareDatetime,
    BaseModel,
    ConfigDict,
    Field,
    ValidationError,
    field_serializer,
    field_validator,
)

from .bindings import (
    RoomContext,
    RoomRequest,
    RoomWebSocket,
    RoomWebSocketPairFactory,
)
from .errors import (
    ProtocolFault,
    RoomFault,
    StoredStateFault,
    error_envelope,
    http_status_for_fault,
    rpc_failure,
    rpc_success,
)
from .protocol import (
    MAX_CLIENT_FRAME_BYTES,
    MAX_SAFE_INTEGER,
    FrameTooLarge,
    Game2048Snapshot,
    GameDescriptor,
    MoveCommand,
    RoomId,
    ServerErrorMessage,
    ServerSnapshotMessage,
    js_isoformat,
    parse_client_frame,
    parse_room_id,
)
from .storage import RoomStore, SocketIdentity, StorageFault

if sys.platform == "emscripten":
    from js import WebSocketPair  # pyright: ignore[reportMissingImports]
    from workers import DurableObject, Response  # pyright: ignore[reportAssignmentType]
else:
    WebSocketPair: RoomWebSocketPairFactory | None = None

    class DurableObject:
        """Minimal CPython base used by the room boundary tests."""

        def __init__(self, ctx: RoomContext, env: object) -> None:
            self.ctx = ctx
            self.env = env

    class Response:
        """Minimal CPython response preserving the Workers constructor shape."""

        def __init__(
            self,
            body: str | None = None,
            status: int = 200,
            headers: Mapping[str, str] | None = None,
            web_socket: RoomWebSocket | None = None,
        ) -> None:
            self.body = body
            self.status = status
            self.headers = {key.casefold(): value for key, value in (headers or {}).items()}
            self.web_socket = web_socket


ROOM_EXPIRED_SOCKET_CODE = 4000
ROOM_EXPIRED_SOCKET_REASON = "room expired"
REPLACED_SOCKET_CODE = 4001
REPLACED_SOCKET_REASON = "replaced"
POLICY_VIOLATION_CODE = 1008
POLICY_VIOLATION_REASON = "policy violation"
MESSAGE_TOO_LARGE_CODE = 1009
MESSAGE_TOO_LARGE_REASON = "message too large"
INTERNAL_SOCKET_CODE = 1011
SEND_FAILED_REASON = "send failed"
CONNECTION_FAILED_REASON = "connection failed"
TRANSPORT_ERROR_REASON = "transport error"
MAX_INVALID_FRAME_COUNT = 3
OPEN_READY_STATE = 1

_EPOCH = datetime(1970, 1, 1, tzinfo=UTC)


class _StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True, frozen=True)


class CreateRoomInput(_StrictModel):
    room_id: RoomId
    created_by: str = Field(min_length=1)
    now_ms: int = Field(ge=0, le=MAX_SAFE_INTEGER)
    game: GameDescriptor

    @field_validator("now_ms", mode="before")
    @classmethod
    def reject_boolean_time(cls, value: object) -> object:
        if type(value) is not int:
            raise ValueError("now_ms must be an integer")
        return value


class _TicketValue(_StrictModel):
    ticket: str = Field(min_length=1)
    expires_at: AwareDatetime

    @field_serializer("expires_at", when_used="json")
    def serialize_expires_at(self, value: datetime) -> str:
        return js_isoformat(value)


class _SocketAttachment(_StrictModel):
    user_id: str = Field(min_length=1)
    seat: Literal[0, 1]
    invalid_frame_count: int = Field(ge=0, le=MAX_INVALID_FRAME_COUNT)

    @field_validator("seat", "invalid_frame_count", mode="before")
    @classmethod
    def reject_boolean_integer(cls, value: object) -> object:
        if type(value) is not int:
            raise ValueError("attachment integers must be exact")
        return value

    @property
    def identity(self) -> SocketIdentity:
        return SocketIdentity(self.user_id, self.seat)


def _wall_clock_ms() -> int:
    return time.time_ns() // 1_000_000


def _reject_json_constant(_constant: str) -> None:
    raise ValueError("Non-standard JSON constants are not allowed")


def _compact_json(value: object) -> str:
    return json.dumps(value, separators=(",", ":"), allow_nan=False)


def _datetime_to_epoch_ms(value: datetime) -> int:
    normalized = value.astimezone(UTC)
    delta = normalized - _EPOCH
    return ((delta.days * 86_400 + delta.seconds) * 1_000) + (delta.microseconds // 1_000)


def _valid_public_integer(value: object) -> int | None:
    if type(value) is int and 0 <= value <= MAX_SAFE_INTEGER:
        return value
    return None


type _SocketKey = tuple[Literal["js", "python"], int]


def _socket_key(socket: RoomWebSocket) -> _SocketKey:
    if sys.platform == "emscripten":
        return ("js", socket.js_id)
    js_id = getattr(socket, "js_id", None)
    if type(js_id) is int:
        return ("js", js_id)
    return ("python", id(socket))


def _same_socket(left: RoomWebSocket, right: RoomWebSocket) -> bool:
    return _socket_key(left) == _socket_key(right)


class GameRoom(DurableObject):
    """One SQLite-backed, hibernation-safe 2048 room."""

    def __init__(self, ctx: RoomContext, env: object) -> None:
        super().__init__(ctx, env)
        self.store = RoomStore(ctx.storage)
        self.clock_ms: Callable[[], int] = _wall_clock_ms
        self._storage_deleted = False

        async def reconcile_alarm() -> None:
            self.store.initialize_schema()
            deadline = self.store.retained_alarm_deadline()
            if deadline is None:
                return
            current_alarm = await self.ctx.storage.getAlarm()
            if current_alarm != deadline:
                await self.ctx.storage.setAlarm(deadline)

        self.ctx.blockConcurrencyWhile(reconcile_alarm)

    async def create_room_rpc(self, payload: object) -> dict[str, object]:
        sockets = list(self.ctx.getWebSockets())
        try:
            self._prepare_storage_entrypoint()
            requested = CreateRoomInput.model_validate(payload)
            creation = self.store.create_room_with_status(
                room_id=requested.room_id,
                descriptor=requested.game,
                created_by=requested.created_by,
                now_ms=requested.now_ms,
            )
            snapshot = creation.snapshot
            connected = self._connected_user_ids(sockets, snapshot)
            if connected:
                snapshot = self.store.get_snapshot(
                    requested.created_by,
                    connected_user_ids=connected,
                )
            try:
                await self.ctx.storage.setAlarm(_datetime_to_epoch_ms(snapshot.expires_at))
            except Exception:
                if creation.created:
                    try:
                        await self.ctx.storage.deleteAll()
                    except Exception:
                        return rpc_failure(RoomFault.service_unavailable())
                    self._storage_deleted = True
                return rpc_failure(RoomFault.service_unavailable())
            return rpc_success(snapshot.model_dump(mode="json"))
        except ValidationError:
            return rpc_failure(RoomFault.for_code("invalid_request"))
        except RoomFault as fault:
            return rpc_failure(fault)
        except (StorageFault, StoredStateFault):
            return rpc_failure(RoomFault.service_unavailable())
        except Exception:
            return rpc_failure(RoomFault.service_unavailable())

    async def get_snapshot_rpc(self, user_id: object) -> dict[str, object]:
        sockets = list(self.ctx.getWebSockets())
        try:
            self._prepare_storage_entrypoint()
            snapshot = self._snapshot_with_presence(user_id, sockets)
            return rpc_success(snapshot.model_dump(mode="json"))
        except RoomFault as fault:
            return rpc_failure(fault)
        except (StorageFault, StoredStateFault):
            return rpc_failure(RoomFault.service_unavailable())
        except Exception:
            return rpc_failure(RoomFault.service_unavailable())

    async def join_rpc(self, user_id: object, now_ms: object) -> dict[str, object]:
        sockets = list(self.ctx.getWebSockets())
        try:
            self._prepare_storage_entrypoint()
            snapshot = self.store.join(user_id, now_ms)
            connected = self._connected_user_ids(sockets, snapshot)
            if connected:
                snapshot = self.store.get_snapshot(
                    user_id,
                    connected_user_ids=connected,
                )
            return rpc_success(snapshot.model_dump(mode="json"))
        except RoomFault as fault:
            return rpc_failure(fault)
        except (StorageFault, StoredStateFault):
            return rpc_failure(RoomFault.service_unavailable())
        except Exception:
            return rpc_failure(RoomFault.service_unavailable())

    async def issue_ticket_rpc(
        self,
        user_id: object,
        now_ms: object,
    ) -> dict[str, object]:
        try:
            self._prepare_storage_entrypoint()
            issued = self.store.issue_ticket(user_id, now_ms)
            value = _TicketValue(
                ticket=issued.ticket,
                expires_at=issued.expires_at,
            )
            return rpc_success(value.model_dump(mode="json"))
        except RoomFault as fault:
            return rpc_failure(fault)
        except (StorageFault, StoredStateFault):
            return rpc_failure(RoomFault.service_unavailable())
        except Exception:
            return rpc_failure(RoomFault.service_unavailable())

    async def fetch(self, request: RoomRequest) -> Response:
        try:
            self._prepare_storage_entrypoint()
        except Exception:
            return self._fault_response(RoomFault.service_unavailable())
        try:
            parsed = urlsplit(request.url)
        except (TypeError, ValueError):
            return self._fault_response(RoomFault.for_code("room_not_found"))
        path_parts = parsed.path.split("/")
        if (
            len(path_parts) != 5
            or path_parts[:3] != ["", "v1", "rooms"]
            or path_parts[4] != "socket"
        ):
            return self._fault_response(
                RoomFault("room_not_found", "Room route not found")
            )
        try:
            path_room_id = parse_room_id(path_parts[3])
        except RoomFault as fault:
            return self._fault_response(fault)
        if request.method != "GET":
            return self._fault_response(
                RoomFault("invalid_request", "Method not allowed"),
                status=405,
                headers={"allow": "GET"},
            )
        upgrade = request.headers.get("upgrade")
        if upgrade is None or upgrade.casefold() != "websocket":
            return self._fault_response(
                RoomFault("invalid_request", "WebSocket upgrade required"),
                status=426,
                headers={"upgrade": "websocket"},
            )
        try:
            query = parse_qsl(
                parsed.query,
                keep_blank_values=True,
                strict_parsing=True,
            )
        except ValueError:
            query = []
        if len(query) != 1 or query[0][0] != "ticket" or not query[0][1]:
            return self._fault_response(RoomFault.for_code("invalid_ticket"))

        try:
            self.store.require_room_id(path_room_id)
        except RoomFault as fault:
            await self._discard_storage_if_empty()
            return self._fault_response(fault)
        except (StorageFault, StoredStateFault):
            return self._fault_response(RoomFault.service_unavailable())
        except Exception:
            return self._fault_response(RoomFault.service_unavailable())

        raw_ticket = query[0][1]
        now_ms = self.clock_ms()
        created_pairs: list[tuple[RoomWebSocket, RoomWebSocket]] = []

        def create_pair(_identity: SocketIdentity) -> tuple[RoomWebSocket, RoomWebSocket]:
            if WebSocketPair is None:
                raise RuntimeError("WebSocketPair is unavailable")
            sockets = WebSocketPair.new().object_values()
            created_pairs.append(sockets)
            return sockets

        def close_created_pairs() -> None:
            for client, server in created_pairs:
                self._close_socket(server, INTERNAL_SOCKET_CODE, CONNECTION_FAILED_REASON)
                self._close_socket(client, INTERNAL_SOCKET_CODE, CONNECTION_FAILED_REASON)

        def accept_pair(
            sockets: tuple[RoomWebSocket, RoomWebSocket],
            identity: SocketIdentity,
        ) -> None:
            _client, server = sockets
            self.ctx.acceptWebSocket(server)
            server.serializeAttachment(
                self._serialize_attachment(
                    _SocketAttachment(
                        user_id=identity.user_id,
                        seat=identity.seat,
                        invalid_frame_count=0,
                    )
                )
            )

        def finish_pair(
            sockets: tuple[RoomWebSocket, RoomWebSocket],
            identity: SocketIdentity,
        ) -> tuple[Response, SocketIdentity, RoomWebSocket, Game2048Snapshot]:
            client, server = sockets
            snapshot = self._snapshot_with_presence(
                identity.user_id,
                list(self.ctx.getWebSockets()),
            )
            if not self._send_payload(server, self._snapshot_payload(snapshot)):
                raise RuntimeError("Initial WebSocket snapshot send failed")
            response = Response(None, status=101, web_socket=client)
            return response, identity, server, snapshot

        try:
            response, identity, server, snapshot = self.store.setup_socket(
                raw_ticket,
                now_ms,
                create=create_pair,
                accept=accept_pair,
                finish=finish_pair,
            )
        except RoomFault as fault:
            close_created_pairs()
            return self._fault_response(fault)
        except StorageFault:
            close_created_pairs()
            return self._fault_response(RoomFault.service_unavailable())
        except Exception:
            close_created_pairs()
            return self._fault_response(
                RoomFault("service_unavailable", "WebSocket connection failed"),
                status=500,
            )

        replaced_sockets: list[RoomWebSocket] = []
        for socket in list(self.ctx.getWebSockets()):
            if _same_socket(socket, server) or not self._is_open(socket):
                continue
            attachment = self._read_attachment(socket)
            if attachment is None or attachment.identity != identity:
                continue
            replaced_sockets.append(socket)

        for socket in replaced_sockets:
            self._close_socket(
                socket,
                REPLACED_SOCKET_CODE,
                REPLACED_SOCKET_REASON,
            )

        self._broadcast_snapshot(
            snapshot,
            list(self.ctx.getWebSockets()),
            excluded=server,
        )
        return response

    async def webSocketMessage(self, socket: RoomWebSocket, frame: object) -> None:
        if self._storage_deleted:
            self._send_fault(
                socket,
                RoomFault.for_code("room_not_found"),
                current_version=0,
            )
            return
        sockets = list(self.ctx.getWebSockets())
        frame_size = self._frame_size(frame)
        if frame_size is not None and frame_size > MAX_CLIENT_FRAME_BYTES:
            self._close_socket(socket, MESSAGE_TOO_LARGE_CODE, MESSAGE_TOO_LARGE_REASON)
            return

        attachment = self._read_attachment(socket)
        if attachment is None:
            self._close_socket(socket, POLICY_VIOLATION_CODE, POLICY_VIOLATION_REASON)
            return

        try:
            base_snapshot = self.store.get_snapshot(attachment.user_id)
        except RoomFault as fault:
            if fault.code == "not_joined":
                self._close_socket(socket, POLICY_VIOLATION_CODE, POLICY_VIOLATION_REASON)
            else:
                self._send_fault(socket, fault, current_version=0)
            return
        except (StorageFault, StoredStateFault):
            self._send_internal(socket, current_version=0)
            return
        except Exception:
            self._send_internal(socket, current_version=0)
            return

        if not self._attachment_matches_snapshot(attachment, base_snapshot):
            self._close_socket(socket, POLICY_VIOLATION_CODE, POLICY_VIOLATION_REASON)
            return
        connected = self._connected_user_ids(sockets, base_snapshot)
        try:
            current_snapshot = self.store.get_snapshot(
                attachment.user_id,
                connected_user_ids=connected,
            )
        except RoomFault as fault:
            self._send_fault(socket, fault, current_version=base_snapshot.version)
            return
        except Exception:
            self._send_internal(socket, current_version=base_snapshot.version)
            return

        try:
            command = parse_client_frame(frame, GameDescriptor(game_type="2048", mode="1v1"))
        except FrameTooLarge:
            self._close_socket(socket, MESSAGE_TOO_LARGE_CODE, MESSAGE_TOO_LARGE_REASON)
            return
        except ProtocolFault as fault:
            invalid_count = min(
                attachment.invalid_frame_count + 1,
                MAX_INVALID_FRAME_COUNT,
            )
            updated_attachment = attachment.model_copy(
                update={"invalid_frame_count": invalid_count}
            )
            if not self._write_attachment(socket, updated_attachment):
                return
            if invalid_count >= MAX_INVALID_FRAME_COUNT:
                self._close_socket(socket, POLICY_VIOLATION_CODE, POLICY_VIOLATION_REASON)
                return
            self._send_error(
                socket,
                code="invalid_request",
                message=fault.message,
                current_version=current_snapshot.version,
            )
            return

        if not self._write_attachment(
            socket,
            attachment.model_copy(update={"invalid_frame_count": 0}),
        ):
            return
        request_id = command.request_id if isinstance(command, MoveCommand) else None

        try:
            result = self.store.execute_command(
                attachment.identity,
                command,
                self.clock_ms(),
                connected_user_ids=connected,
            )
        except RoomFault as fault:
            fresh_snapshot = self._snapshot_after_fault(
                attachment.user_id,
                sockets,
                current_snapshot,
            )
            current_version = _valid_public_integer(fault.details.get("current_version"))
            self._send_fault(
                socket,
                fault,
                current_version=(
                    fresh_snapshot.version if current_version is None else current_version
                ),
                current_board_version=_valid_public_integer(
                    fault.details.get("current_board_version")
                ),
                request_id=request_id,
            )
            return
        except (StorageFault, StoredStateFault):
            fresh = self._snapshot_after_fault(
                attachment.user_id,
                sockets,
                current_snapshot,
            )
            self._send_internal(
                socket,
                current_version=fresh.version,
                request_id=request_id,
            )
            return
        except Exception:
            fresh = self._snapshot_after_fault(
                attachment.user_id,
                sockets,
                current_snapshot,
            )
            self._send_internal(
                socket,
                current_version=fresh.version,
                request_id=request_id,
            )
            return

        if result.next_alarm is not None:
            with suppress(Exception):
                await self.ctx.storage.setAlarm(result.next_alarm)
        if result.audience == "actor":
            self._send_snapshot(socket, result.snapshot, request_id=request_id)
            return
        self._broadcast_snapshot(
            result.snapshot,
            sockets,
            request_id=request_id,
        )

    async def webSocketClose(
        self,
        socket: RoomWebSocket,
        code: int,
        reason: str,
        _was_clean: bool,
    ) -> None:
        self._persist_final_disconnect(socket, code, reason)

    async def webSocketError(self, socket: RoomWebSocket, _error: object) -> None:
        self._persist_final_disconnect(
            socket,
            INTERNAL_SOCKET_CODE,
            TRANSPORT_ERROR_REASON,
        )

    async def alarm(self) -> None:
        if self._storage_deleted:
            return
        sockets = list(self.ctx.getWebSockets())
        connected = self._connected_ids_for_alarm(sockets)
        result = self.store.expire(
            self.clock_ms(),
            connected_user_ids=connected,
        )
        if result.kind == "none":
            return
        if result.kind == "reschedule":
            if result.next_alarm is None:
                raise StorageFault
            await self.ctx.storage.setAlarm(result.next_alarm)
            return
        if result.snapshot is None:
            raise StorageFault

        payload = self._snapshot_payload(result.snapshot)
        for socket in sockets:
            if not self._is_open(socket):
                continue
            with suppress(Exception):
                socket.send(payload)
        for socket in sockets:
            self._close_socket(
                socket,
                ROOM_EXPIRED_SOCKET_CODE,
                ROOM_EXPIRED_SOCKET_REASON,
            )
        await self.ctx.storage.deleteAll()
        self._storage_deleted = True

    def _snapshot_with_presence(
        self,
        user_id: object,
        sockets: Collection[RoomWebSocket],
    ) -> Game2048Snapshot:
        snapshot = self.store.get_snapshot(user_id)
        connected = self._connected_user_ids(sockets, snapshot)
        if not connected:
            return snapshot
        return self.store.get_snapshot(
            user_id,
            connected_user_ids=connected,
        )

    async def _discard_storage_if_empty(self) -> None:
        """Drop storage this object holds only because a request named it."""
        if self._storage_deleted:
            return
        try:
            if list(self.ctx.getWebSockets()):
                return
            # Synchronous read immediately before the delete; an await here
            # would let a concurrent create_room_rpc land in between.
            if self.store.holds_room():
                return
            await self.ctx.storage.deleteAll()
        except Exception:
            # Best effort: a clean 404 must not become a 503.
            return
        self._storage_deleted = True

    def _prepare_storage_entrypoint(self) -> None:
        if not self._storage_deleted:
            return
        self.store.initialize_schema()
        self._storage_deleted = False

    def _snapshot_after_fault(
        self,
        user_id: str,
        sockets: Collection[RoomWebSocket],
        fallback: Game2048Snapshot,
    ) -> Game2048Snapshot:
        try:
            return self._snapshot_with_presence(user_id, sockets)
        except Exception:
            return fallback

    def _connected_ids_for_alarm(
        self,
        sockets: Collection[RoomWebSocket],
    ) -> frozenset[str]:
        for socket in sockets:
            if not self._is_open(socket):
                continue
            attachment = self._read_attachment(socket)
            if attachment is None:
                continue
            try:
                snapshot = self.store.get_snapshot(attachment.user_id)
            except RoomFault:
                continue
            if self._attachment_matches_snapshot(attachment, snapshot):
                return self._connected_user_ids(sockets, snapshot)
        return frozenset()

    def _connected_user_ids(
        self,
        sockets: Collection[RoomWebSocket],
        snapshot: Game2048Snapshot,
        *,
        excluded: RoomWebSocket | None = None,
    ) -> frozenset[str]:
        connected: set[str] = set()
        for socket in sockets:
            if (
                (excluded is not None and _same_socket(socket, excluded))
                or not self._is_open(socket)
            ):
                continue
            attachment = self._read_attachment(socket)
            if attachment is None:
                continue
            if self._attachment_matches_snapshot(attachment, snapshot):
                connected.add(attachment.user_id)
        return frozenset(connected)

    @staticmethod
    def _attachment_matches_snapshot(
        attachment: _SocketAttachment,
        snapshot: Game2048Snapshot,
    ) -> bool:
        return any(
            participant.user_id == attachment.user_id and participant.seat == attachment.seat
            for participant in snapshot.participants
        )

    def _has_open_identity(
        self,
        sockets: Collection[RoomWebSocket],
        identity: SocketIdentity,
        *,
        excluded: RoomWebSocket | None = None,
    ) -> bool:
        for socket in sockets:
            if (
                (excluded is not None and _same_socket(socket, excluded))
                or not self._is_open(socket)
            ):
                continue
            attachment = self._read_attachment(socket)
            if attachment is not None and attachment.identity == identity:
                return True
        return False

    def _persist_final_disconnect(
        self,
        socket: RoomWebSocket,
        code: int,
        reason: str,
    ) -> None:
        if self._storage_deleted:
            self._close_socket(socket, code, reason)
            return
        sockets = list(self.ctx.getWebSockets())
        attachment = self._read_attachment(socket)
        self._close_socket(socket, code, reason)
        if attachment is None:
            return
        try:
            snapshot = self.store.get_snapshot(attachment.user_id)
            if not self._attachment_matches_snapshot(attachment, snapshot):
                return
            if self._has_open_identity(
                sockets,
                attachment.identity,
                excluded=socket,
            ):
                return
            self.store.mark_disconnected(attachment.identity, self.clock_ms())
            connected = self._connected_user_ids(
                sockets,
                snapshot,
                excluded=socket,
            )
            updated = self.store.get_snapshot(
                attachment.user_id,
                connected_user_ids=connected,
            )
            self._broadcast_snapshot(updated, sockets, excluded=socket)
        except Exception:
            return

    def _broadcast_snapshot(
        self,
        snapshot: Game2048Snapshot,
        sockets: Collection[RoomWebSocket],
        *,
        request_id: str | None = None,
        excluded: RoomWebSocket | None = None,
    ) -> None:
        payload = self._snapshot_payload(snapshot, request_id=request_id)
        for socket in sockets:
            if (
                (excluded is not None and _same_socket(socket, excluded))
                or not self._is_open(socket)
            ):
                continue
            attachment = self._read_attachment(socket)
            if attachment is None:
                continue
            if not self._attachment_matches_snapshot(attachment, snapshot):
                continue
            self._send_payload(socket, payload)

    def _send_snapshot(
        self,
        socket: RoomWebSocket,
        snapshot: Game2048Snapshot,
        *,
        request_id: str | None = None,
    ) -> None:
        self._send_payload(
            socket,
            self._snapshot_payload(snapshot, request_id=request_id),
        )

    def _snapshot_payload(
        self,
        snapshot: Game2048Snapshot,
        *,
        request_id: str | None = None,
    ) -> str:
        if request_id is None:
            message = ServerSnapshotMessage(snapshot=snapshot)
        else:
            message = ServerSnapshotMessage(
                snapshot=snapshot,
                request_id=request_id,
            )
        return _compact_json(message.model_dump(mode="json"))

    def _send_fault(
        self,
        socket: RoomWebSocket,
        fault: RoomFault,
        *,
        current_version: int,
        current_board_version: int | None = None,
        request_id: str | None = None,
    ) -> None:
        self._send_error(
            socket,
            code=fault.code,
            message=fault.message,
            current_version=current_version,
            current_board_version=current_board_version,
            request_id=request_id,
        )

    def _send_internal(
        self,
        socket: RoomWebSocket,
        *,
        current_version: int,
        request_id: str | None = None,
    ) -> None:
        self._send_fault(
            socket,
            RoomFault("service_unavailable", "Room command failed"),
            current_version=current_version,
            request_id=request_id,
        )

    def _send_error(
        self,
        socket: RoomWebSocket,
        *,
        code: str,
        message: str,
        current_version: int,
        current_board_version: int | None = None,
        request_id: str | None = None,
    ) -> None:
        fields: dict[str, object] = {
            "code": code,
            "message": message,
            "current_version": current_version,
        }
        if current_board_version is not None:
            fields["current_board_version"] = current_board_version
        if request_id is not None:
            fields["request_id"] = request_id
        error = ServerErrorMessage.model_validate(fields)
        self._send_payload(
            socket,
            _compact_json(error.model_dump(mode="json")),
        )

    def _send_payload(self, socket: RoomWebSocket, payload: str) -> bool:
        if not self._is_open(socket):
            return False
        try:
            socket.send(payload)
        except Exception:
            self._close_socket(socket, INTERNAL_SOCKET_CODE, SEND_FAILED_REASON)
            return False
        return True

    @staticmethod
    def _close_socket(socket: RoomWebSocket, code: int, reason: str) -> bool:
        try:
            socket.close(code, reason)
        except Exception:
            return False
        return True

    @staticmethod
    def _is_open(socket: RoomWebSocket) -> bool:
        return type(socket.readyState) is int and socket.readyState == OPEN_READY_STATE

    def _read_attachment(self, socket: RoomWebSocket) -> _SocketAttachment | None:
        try:
            raw_attachment = socket.deserializeAttachment()
            if not isinstance(raw_attachment, str):
                return None
            payload = json.loads(
                raw_attachment,
                parse_constant=_reject_json_constant,
            )
            return _SocketAttachment.model_validate(payload)
        except (TypeError, ValueError, ValidationError):
            return None
        except Exception:
            return None

    def _write_attachment(
        self,
        socket: RoomWebSocket,
        attachment: _SocketAttachment,
    ) -> bool:
        try:
            socket.serializeAttachment(self._serialize_attachment(attachment))
        except Exception:
            self._close_socket(socket, INTERNAL_SOCKET_CODE, SEND_FAILED_REASON)
            return False
        return True

    @staticmethod
    def _serialize_attachment(attachment: _SocketAttachment) -> str:
        return _compact_json(attachment.model_dump(mode="json"))

    @staticmethod
    def _frame_size(frame: object) -> int | None:
        if isinstance(frame, str):
            try:
                return len(frame.encode("utf-8"))
            except UnicodeError:
                return None
        if isinstance(frame, memoryview):
            return frame.nbytes
        if isinstance(frame, (bytes, bytearray)):
            return len(frame)
        byte_length = getattr(frame, "byteLength", None)
        if type(byte_length) is int and byte_length >= 0:
            return byte_length
        return None

    @staticmethod
    def _fault_response(
        fault: RoomFault,
        *,
        status: int | None = None,
        headers: Mapping[str, str] | None = None,
    ) -> Response:
        response_headers = {"content-type": "application/json"}
        response_headers.update(headers or {})
        return Response(
            _compact_json(error_envelope(fault)),
            status=(http_status_for_fault(fault) if status is None else status),
            headers=response_headers,
        )


__all__ = ["CreateRoomInput", "GameRoom", "Response"]
