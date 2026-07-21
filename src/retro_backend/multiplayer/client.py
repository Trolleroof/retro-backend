"""Strict FastAPI-to-Durable-Object RPC adapter for multiplayer rooms."""

import json
from datetime import datetime
from typing import Annotated, Literal, Protocol, cast

from fastapi import Request
from pydantic import (
    AwareDatetime,
    BaseModel,
    ConfigDict,
    Field,
    TypeAdapter,
    field_serializer,
)

from .errors import RoomFault, RPCFailure
from .protocol import Game2048Snapshot, GameDescriptor, js_isoformat
from .tickets import TICKET_PATTERN


class RoomStub(Protocol):
    """Structural RPC surface exposed by one named room object."""

    async def create_room_rpc(self, payload: object) -> object: ...

    async def get_snapshot_rpc(self, user_id: object) -> object: ...

    async def join_rpc(self, user_id: object, now_ms: object) -> object: ...

    async def issue_ticket_rpc(self, user_id: object, now_ms: object) -> object: ...


class RoomNamespace(Protocol):
    """Structural Cloudflare namespace used to address rooms by name."""

    def getByName(self, room_id: str) -> RoomStub: ...


class _RoomEnvironment(Protocol):
    GAME_ROOMS: RoomNamespace


class _StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True, frozen=True)


class RoomTicket(_StrictModel):
    """Strict public value returned by the ticket RPC."""

    ticket: str = Field(pattern=TICKET_PATTERN)
    expires_at: AwareDatetime

    @field_serializer("expires_at", when_used="json")
    def serialize_expires_at(self, value: datetime) -> str:
        return js_isoformat(value)


class _SnapshotRPCSuccess(_StrictModel):
    ok: Literal[True]
    value: Game2048Snapshot


class _TicketRPCSuccess(_StrictModel):
    ok: Literal[True]
    value: RoomTicket


type _SnapshotRPCEnvelope = Annotated[
    _SnapshotRPCSuccess | RPCFailure,
    Field(discriminator="ok"),
]
type _TicketRPCEnvelope = Annotated[
    _TicketRPCSuccess | RPCFailure,
    Field(discriminator="ok"),
]

_SNAPSHOT_RPC_ADAPTER = TypeAdapter(_SnapshotRPCEnvelope)
_TICKET_RPC_ADAPTER = TypeAdapter(_TicketRPCEnvelope)


def _rpc_json(value: object) -> str:
    if not isinstance(value, dict) or type(value.get("ok")) is not bool:
        raise RoomFault.service_unavailable()
    try:
        return json.dumps(
            value,
            allow_nan=False,
            ensure_ascii=True,
            separators=(",", ":"),
        )
    except Exception as error:
        raise RoomFault.service_unavailable() from error


def _decode_snapshot(value: object) -> Game2048Snapshot:
    try:
        envelope = _SNAPSHOT_RPC_ADAPTER.validate_json(_rpc_json(value))
    except RoomFault:
        raise
    except Exception as error:
        raise RoomFault.service_unavailable() from error
    if isinstance(envelope, RPCFailure):
        raise RoomFault(envelope.error.code, envelope.error.message)
    return envelope.value


def _decode_ticket(value: object) -> RoomTicket:
    try:
        envelope = _TICKET_RPC_ADAPTER.validate_json(_rpc_json(value))
    except RoomFault:
        raise
    except Exception as error:
        raise RoomFault.service_unavailable() from error
    if isinstance(envelope, RPCFailure):
        raise RoomFault(envelope.error.code, envelope.error.message)
    return envelope.value


class RoomClient:
    """Retained room stub with fail-closed RPC decoding."""

    def __init__(self, room_id: str, stub: RoomStub) -> None:
        self.room_id = room_id
        self._stub = stub

    @classmethod
    def from_request(cls, request: Request, room_id: str) -> "RoomClient":
        try:
            environment = cast(_RoomEnvironment, request.scope["env"])
            stub = environment.GAME_ROOMS.getByName(room_id)
        except Exception as error:
            raise RoomFault.service_unavailable() from error
        return cls(room_id, stub)

    def _validate_snapshot(self, snapshot: Game2048Snapshot) -> Game2048Snapshot:
        if snapshot.room_id != self.room_id:
            raise RoomFault.service_unavailable()
        return snapshot

    async def create(
        self,
        room_id: str,
        created_by: str,
        now_ms: int,
        game: GameDescriptor,
    ) -> Game2048Snapshot:
        if room_id != self.room_id:
            raise RoomFault.service_unavailable()
        payload = {
            "room_id": room_id,
            "created_by": created_by,
            "now_ms": now_ms,
            "game": game.model_dump(mode="json"),
        }
        try:
            result = await self._stub.create_room_rpc(payload)
        except Exception as error:
            raise RoomFault.service_unavailable() from error
        return self._validate_snapshot(_decode_snapshot(result))

    async def get_snapshot(self, user_id: str) -> Game2048Snapshot:
        try:
            result = await self._stub.get_snapshot_rpc(user_id)
        except Exception as error:
            raise RoomFault.service_unavailable() from error
        return self._validate_snapshot(_decode_snapshot(result))

    async def join(self, user_id: str, now_ms: int) -> Game2048Snapshot:
        try:
            result = await self._stub.join_rpc(user_id, now_ms)
        except Exception as error:
            raise RoomFault.service_unavailable() from error
        return self._validate_snapshot(_decode_snapshot(result))

    async def issue_ticket(self, user_id: str, now_ms: int) -> RoomTicket:
        try:
            result = await self._stub.issue_ticket_rpc(user_id, now_ms)
        except Exception as error:
            raise RoomFault.service_unavailable() from error
        return _decode_ticket(result)


__all__ = ["RoomClient", "RoomNamespace", "RoomStub", "RoomTicket"]
