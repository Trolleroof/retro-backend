"""Stable public faults and RPC envelopes for multiplayer rooms."""

from collections.abc import Mapping
from types import MappingProxyType
from typing import Literal, Self, cast

from pydantic import BaseModel, ConfigDict, Field

RoomErrorCode = Literal[
    "unauthorized",
    "service_unavailable",
    "invalid_request",
    "room_not_found",
    "room_full",
    "not_joined",
    "room_expired",
    "match_not_active",
    "stale_version",
    "stale_board_version",
    "invalid_ticket",
    "ticket_expired",
    "ticket_consumed",
]

HTTP_STATUS_BY_ERROR_CODE: Mapping[RoomErrorCode, int] = MappingProxyType(
    {
        "invalid_request": 400,
        "invalid_ticket": 401,
        "ticket_expired": 401,
        "ticket_consumed": 401,
        "unauthorized": 401,
        "not_joined": 403,
        "room_not_found": 404,
        "room_full": 409,
        "room_expired": 410,
        "service_unavailable": 503,
    }
)

PUBLIC_ERROR_MESSAGES: Mapping[RoomErrorCode, str] = MappingProxyType(
    {
        "unauthorized": "Unauthorized",
        "service_unavailable": "Room service unavailable",
        "invalid_request": "Invalid request",
        "room_not_found": "Room not found",
        "room_full": "Room is full",
        "not_joined": "User has not joined this room",
        "room_expired": "Room has expired",
        "match_not_active": "Match is not active",
        "stale_version": "Room version is stale",
        "stale_board_version": "Board version is stale",
        "invalid_ticket": "Invalid ticket",
        "ticket_expired": "Ticket has expired",
        "ticket_consumed": "Ticket has already been consumed",
    }
)


class RoomFault(Exception):
    """A deliberately public room failure safe to return to a client."""

    code: RoomErrorCode
    message: str

    def __init__(
        self,
        code: RoomErrorCode,
        message: str,
        *,
        details: Mapping[str, object] | None = None,
    ) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.details: Mapping[str, object] = MappingProxyType(dict(details or {}))

    @classmethod
    def for_code(
        cls,
        code: RoomErrorCode,
        *,
        details: Mapping[str, object] | None = None,
    ) -> Self:
        return cls(code, PUBLIC_ERROR_MESSAGES[code], details=details)

    @classmethod
    def service_unavailable(cls) -> Self:
        return cls.for_code("service_unavailable")


class ProtocolFault(ValueError):
    """A stable client-frame rejection safe to send over a socket."""

    code: RoomErrorCode
    message: str

    def __init__(
        self,
        message: str,
        *,
        code: RoomErrorCode = "invalid_request",
        close_code: int = 1008,
    ) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.close_code = close_code


class StoredStateFault(RuntimeError):
    """Signals corrupt private state that must fail closed."""


class _StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True, frozen=True)


class ErrorDetail(_StrictModel):
    code: RoomErrorCode
    message: str = Field(min_length=1)

    @classmethod
    def from_fault(cls, fault: RoomFault) -> Self:
        return cls(code=fault.code, message=fault.message)


class ErrorEnvelope(_StrictModel):
    error: ErrorDetail

    @classmethod
    def from_fault(cls, fault: RoomFault) -> Self:
        return cls(error=ErrorDetail.from_fault(fault))


class RPCSuccess(_StrictModel):
    ok: Literal[True] = True
    value: object


class RPCFailure(_StrictModel):
    ok: Literal[False] = False
    error: ErrorDetail


def error_envelope(fault: RoomFault) -> dict[str, object]:
    """Serialize an HTTP failure without exception causes or private details."""
    return cast(dict[str, object], ErrorEnvelope.from_fault(fault).model_dump())


def http_status_for_fault(fault: RoomFault) -> int:
    """Return the stable HTTP status for a public room fault."""
    return HTTP_STATUS_BY_ERROR_CODE.get(fault.code, 400)


def rpc_success(value: object) -> dict[str, object]:
    """Build the structured success shape used across Durable Object RPC."""
    return cast(dict[str, object], RPCSuccess(value=value).model_dump())


def rpc_failure(fault: RoomFault) -> dict[str, object]:
    """Build the structured failure shape using only public fault fields."""
    return cast(
        dict[str, object],
        RPCFailure(error=ErrorDetail.from_fault(fault)).model_dump(),
    )
