"""Strict public contracts and client-frame parsing for multiplayer rooms."""

import json
import re
import sys
from collections.abc import Mapping
from datetime import UTC, datetime
from ipaddress import IPv6Address
from typing import Annotated, Literal, Never, Protocol, Self, cast
from urllib.parse import urlsplit

import idna
from pydantic import (
    AwareDatetime,
    BaseModel,
    ConfigDict,
    Field,
    SerializerFunctionWrapHandler,
    ValidationError,
    field_serializer,
    field_validator,
    model_serializer,
    model_validator,
)

from .errors import ProtocolFault, RoomErrorCode, RoomFault

if sys.platform == "emscripten":
    from js import URL as _worker_url  # pyright: ignore[reportMissingImports]
else:
    _worker_url = None

_JS_URL: object | None = _worker_url

MAX_SAFE_INTEGER = 9_007_199_254_740_991
MAX_CLIENT_FRAME_BYTES = 4 * 1024
REQUEST_ID_PATTERN = r"^[A-Za-z0-9_-]{1,64}$"
ROOM_ID_PATTERN = (
    r"^[0-9a-f]{8}-[0-9a-f]{4}-[1-8][0-9a-f]{3}-"
    r"[89ab][0-9a-f]{3}-[0-9a-f]{12}$"
)

_ROOM_ID_RE = re.compile(ROOM_ID_PATTERN)
_ASCII_DECIMAL = frozenset("0123456789")
_ASCII_HEXADECIMAL = frozenset("0123456789abcdefABCDEF")
_ASCII_OCTAL = frozenset("01234567")
_FORBIDDEN_DOMAIN_CHARACTERS = frozenset("#%/:<>?@[\\]^|")
_MAX_ORIGIN_CODE_POINTS = 32_768
_MAX_HOST_CODE_POINTS = 32_768
_MAX_HOST_BYTES = 32_768

SafeInteger = Annotated[int, Field(ge=0, le=MAX_SAFE_INTEGER)]
RoomId = Annotated[str, Field(pattern=ROOM_ID_PATTERN, max_length=36)]
RequestId = Annotated[str, Field(pattern=REQUEST_ID_PATTERN, max_length=64)]
RoomStatus = Literal["waiting", "active", "completed", "expired"]
Direction = Literal["up", "down", "left", "right"]


class _JsURL(Protocol):
    @property
    def protocol(self) -> object: ...

    @property
    def username(self) -> object: ...

    @property
    def password(self) -> object: ...

    @property
    def pathname(self) -> object: ...

    @property
    def search(self) -> object: ...

    @property
    def hash(self) -> object: ...

    @property
    def origin(self) -> object: ...


class _JsURLConstructor(Protocol):
    def new(self, value: str) -> _JsURL: ...


def _serialize_ipv6(address: IPv6Address) -> str:
    hextets = [int.from_bytes(address.packed[index : index + 2]) for index in range(0, 16, 2)]
    best_start = -1
    best_length = 1
    index = 0
    while index < len(hextets):
        if hextets[index] != 0:
            index += 1
            continue
        end = index
        while end < len(hextets) and hextets[end] == 0:
            end += 1
        length = end - index
        if length > best_length:
            best_start = index
            best_length = length
        index = end

    if best_start < 0:
        return ":".join(f"{hextet:x}" for hextet in hextets)
    before = ":".join(f"{hextet:x}" for hextet in hextets[:best_start])
    after = ":".join(f"{hextet:x}" for hextet in hextets[best_start + best_length :])
    return f"{before}::{after}"


def _percent_decode_host(hostname: str) -> str | None:
    if len(hostname) > _MAX_HOST_CODE_POINTS:
        return None

    decoded = bytearray()
    index = 0
    while index < len(hostname):
        character = hostname[index]
        if character == "%":
            if (
                index + 2 >= len(hostname)
                or hostname[index + 1] not in _ASCII_HEXADECIMAL
                or hostname[index + 2] not in _ASCII_HEXADECIMAL
            ):
                return None
            decoded.append(int(hostname[index + 1 : index + 3], 16))
            index += 3
        else:
            try:
                decoded.extend(character.encode("utf-8"))
            except UnicodeEncodeError:
                return None
            index += 1
        if len(decoded) > _MAX_HOST_BYTES:
            return None

    try:
        value = bytes(decoded).decode("utf-8")
    except UnicodeDecodeError:
        return None
    if any(
        ord(character) <= 0x20
        or ord(character) == 0x7F
        or character in _FORBIDDEN_DOMAIN_CHARACTERS
        for character in value
    ):
        return None
    return value


def _parse_ipv4_number(value: str) -> int | None:
    if not value:
        return None

    radix = 10
    digits = value
    allowed_digits = _ASCII_DECIMAL
    max_significant_digits = 10
    if len(value) >= 2 and value[:2].casefold() == "0x":
        radix = 16
        digits = value[2:]
        allowed_digits = _ASCII_HEXADECIMAL
        max_significant_digits = 8
    elif len(value) >= 2 and value[0] == "0":
        radix = 8
        digits = value[1:]
        allowed_digits = _ASCII_OCTAL
        max_significant_digits = 11

    if not digits:
        return 0
    if any(character not in allowed_digits for character in digits):
        return None
    if len(digits.lstrip("0")) > max_significant_digits:
        return None
    return int(digits, radix)


def _ends_in_number(domain: str) -> bool:
    parts = domain.split(".")
    if len(parts) > 1 and not parts[-1]:
        parts.pop()
    if not parts or not parts[-1]:
        return False
    last = parts[-1]
    if all(character in _ASCII_DECIMAL for character in last):
        return True
    if len(last) >= 2 and last[:2].casefold() == "0x":
        return all(character in _ASCII_HEXADECIMAL for character in last[2:])
    return _parse_ipv4_number(last) is not None


def _parse_ipv4(domain: str) -> int | None:
    parts = domain.split(".")
    if parts and not parts[-1]:
        parts.pop()
    if not parts or len(parts) > 4:
        return None

    numbers: list[int] = []
    for part in parts:
        number = _parse_ipv4_number(part)
        if number is None:
            return None
        numbers.append(number)

    if any(number > 255 for number in numbers[:-1]):
        return None
    if numbers[-1] >= 256 ** (5 - len(numbers)):
        return None

    value = numbers[-1]
    for index, number in enumerate(numbers[:-1]):
        value += number * 256 ** (3 - index)
    return value


def _serialize_ipv4(address: int) -> str:
    return ".".join(
        str((address >> shift) & 0xFF)
        for shift in (24, 16, 8, 0)
    )


def _canonical_js_http_origin(candidate: str) -> str | None:
    try:
        parsed = cast(_JsURLConstructor, _JS_URL).new(candidate)
        if (
            parsed.protocol not in {"http:", "https:"}
            or parsed.username != ""
            or parsed.password != ""
            or parsed.pathname != "/"
            or parsed.search != ""
            or parsed.hash != ""
        ):
            return None
        origin = parsed.origin
    except Exception:
        return None
    return origin if isinstance(origin, str) else None


def _canonical_domain(decoded_hostname: str) -> str | None:
    try:
        mapped = idna.uts46_remap(
            decoded_hostname,
            std3_rules=False,
            transitional=False,
        )
    except (UnicodeError, idna.IDNAError):
        return None

    if "_" not in mapped:
        try:
            return idna.encode(
                mapped,
                uts46=False,
                transitional=False,
            ).decode("ascii").casefold()
        except (UnicodeError, idna.IDNAError):
            return None

    try:
        canonical = mapped.encode("ascii").decode("ascii").casefold()
    except UnicodeError:
        return None
    labels = canonical[:-1].split(".") if canonical.endswith(".") else canonical.split(".")
    if (
        not labels
        or any(not label or len(label) > 63 for label in labels)
        or len(canonical.rstrip(".")) > 253
    ):
        return None
    for label in labels:
        if label.startswith("xn--"):
            return None
        try:
            idna.encode(
                label.replace("_", "a"),
                uts46=False,
                transitional=False,
            )
        except (UnicodeError, idna.IDNAError):
            return None
    return canonical


def _canonical_python_http_origin(candidate: str) -> str | None:
    try:
        parsed = urlsplit(candidate)
        scheme = parsed.scheme.casefold()
        hostname = parsed.hostname
        port = parsed.port
    except (TypeError, ValueError):
        return None
    if (
        scheme not in {"http", "https"}
        or not parsed.netloc
        or hostname is None
        or parsed.username is not None
        or parsed.password is not None
        or parsed.path not in {"", "/"}
        or parsed.query
        or parsed.fragment
    ):
        return None

    if ":" in hostname and "%" in hostname:
        return None
    try:
        address = IPv6Address(hostname)
    except ValueError:
        decoded_hostname = _percent_decode_host(hostname)
        if decoded_hostname is None:
            return None
        canonical_domain = _canonical_domain(decoded_hostname)
        if not canonical_domain:
            return None
        if _ends_in_number(canonical_domain):
            ipv4_address = _parse_ipv4(canonical_domain)
            if ipv4_address is None:
                return None
            canonical_host = _serialize_ipv4(ipv4_address)
        else:
            canonical_host = canonical_domain
    else:
        canonical_host = f"[{_serialize_ipv6(address)}]"

    default_port = 80 if scheme == "http" else 443
    suffix = "" if port is None or port == default_port else f":{port}"
    return f"{scheme}://{canonical_host}{suffix}"


def canonical_http_origin(value: object) -> str | None:
    if not isinstance(value, str):
        return None
    candidate = value.strip("\t\n\r ")
    if not candidate or len(candidate) > _MAX_ORIGIN_CODE_POINTS:
        return None
    if _JS_URL is not None:
        return _canonical_js_http_origin(candidate)
    return _canonical_python_http_origin(candidate)


def js_isoformat(value: datetime) -> str:
    """Serialize one aware timestamp like JavaScript Date.toISOString()."""
    normalized = value.astimezone(UTC)
    milliseconds = normalized.microsecond // 1_000
    return f"{normalized:%Y-%m-%dT%H:%M:%S}.{milliseconds:03d}Z"


def _freeze_sequence(value: object) -> object:
    if isinstance(value, list):
        return tuple(value)
    return value


def _freeze_grid(value: object) -> object:
    if not isinstance(value, (list, tuple)):
        return value
    return tuple(
        tuple(row) if isinstance(row, list) else row
        for row in value
    )


class StrictModel(BaseModel):
    """Shared exact, strict, and frozen configuration for public models."""

    model_config = ConfigDict(extra="forbid", strict=True, frozen=True)


class _CorrelatedModel(StrictModel):
    request_id: RequestId | None = None

    @model_validator(mode="before")
    @classmethod
    def reject_explicit_null_request_id(cls, value: object) -> object:
        if (
            isinstance(value, Mapping)
            and "request_id" in value
            and value["request_id"] is None
        ):
            raise ValueError("request_id must be omitted or valid")
        return value

    @model_serializer(mode="wrap")
    def omit_absent_request_id(
        self, handler: SerializerFunctionWrapHandler
    ) -> dict[str, object]:
        serialized = cast(dict[str, object], handler(self))
        if self.request_id is None:
            serialized.pop("request_id", None)
        return serialized


class GameDescriptor(StrictModel):
    game_type: Literal["2048"]
    mode: Literal["1v1"]


class ReadyCommand(StrictModel):
    type: Literal["ready"] = "ready"
    expected_version: SafeInteger


class MoveCommand(_CorrelatedModel):
    type: Literal["move"] = "move"
    direction: Direction
    expected_board_version: SafeInteger


type ClientCommand = ReadyCommand | MoveCommand


class FrameTooLarge(ProtocolFault):
    """An oversized frame that the socket boundary will close with code 1009."""

    def __init__(self) -> None:
        super().__init__(
            "Client frame exceeds 4096 UTF-8 bytes",
            code="invalid_request",
            close_code=1009,
        )


def parse_room_id(value: object) -> str:
    """Return a canonical room UUID or the non-enumerating public fault."""
    if not isinstance(value, str) or _ROOM_ID_RE.fullmatch(value) is None:
        raise RoomFault.for_code("room_not_found")
    return value


def _reject_nonstandard_json_constant(_constant: str) -> Never:
    raise ValueError("Non-standard JSON constants are not allowed")


def parse_client_frame(frame: object, descriptor: GameDescriptor) -> ClientCommand:
    """Parse one exact text command from an untrusted frame."""
    _ = descriptor
    if not isinstance(frame, str):
        raise ProtocolFault("Client frames must be text")
    try:
        encoded = frame.encode("utf-8")
    except UnicodeError as error:
        raise ProtocolFault("Client frame must contain valid JSON") from error
    if len(encoded) > MAX_CLIENT_FRAME_BYTES:
        raise FrameTooLarge
    try:
        payload = json.loads(frame, parse_constant=_reject_nonstandard_json_constant)
    except (UnicodeError, ValueError) as error:
        raise ProtocolFault("Client frame must contain valid JSON") from error
    if not isinstance(payload, dict):
        raise ProtocolFault("Client command must be an object with a type")
    command_payload = cast(dict[str, object], payload)
    command_type = command_payload.get("type")
    if not isinstance(command_type, str):
        raise ProtocolFault("Client command must be an object with a type")

    if command_type == "ready":
        return _validate_command(ReadyCommand, command_payload, "Invalid ready command")
    if command_type == "move":
        return _validate_command(MoveCommand, command_payload, "Invalid move command")
    raise ProtocolFault("Unknown client command type")


def _validate_command[CommandT: StrictModel](
    command_model: type[CommandT],
    payload: dict[str, object],
    message: str,
) -> CommandT:
    try:
        return command_model.model_validate(payload)
    except ValidationError as error:
        raise ProtocolFault(message) from error


class Game2048Participant(StrictModel):
    user_id: str = Field(min_length=1)
    seat: Annotated[int, Field(ge=0, le=1)]
    ready: bool
    connected: bool


class CommonRoomFields(StrictModel):
    room_id: RoomId
    status: RoomStatus
    created_at: AwareDatetime
    expires_at: AwareDatetime
    version: SafeInteger

    @field_serializer("created_at", "expires_at", when_used="json")
    def serialize_timestamp(self, value: datetime) -> str:
        return js_isoformat(value)

    @model_validator(mode="after")
    def validate_time_order(self) -> Self:
        if self.expires_at < self.created_at:
            raise ValueError("expires_at must not precede created_at")
        return self


Tile = Annotated[int, Field(ge=0, le=MAX_SAFE_INTEGER)]


class Game2048BoardSnapshot(StrictModel):
    seat: Annotated[int, Field(ge=0, le=1)]
    board: tuple[tuple[Tile, ...], ...]
    score: SafeInteger
    highest_tile: Tile
    board_version: SafeInteger
    finished: bool

    @field_validator("board", mode="before")
    @classmethod
    def freeze_board(cls, value: object) -> object:
        return _freeze_grid(value)

    @field_validator("board")
    @classmethod
    def validate_board(
        cls, board: tuple[tuple[int, ...], ...]
    ) -> tuple[tuple[int, ...], ...]:
        if len(board) != 4 or any(len(row) != 4 for row in board):
            raise ValueError("2048 board must be 4 by 4")
        if any(
            tile != 0 and (tile < 2 or tile & (tile - 1))
            for row in board
            for tile in row
        ):
            raise ValueError("2048 tiles must be zero or powers of two")
        return board

    @model_validator(mode="after")
    def validate_highest_tile(self) -> Self:
        if self.highest_tile != max(max(row) for row in self.board):
            raise ValueError("highest_tile must equal the board's maximum tile")
        return self


class Game2048Snapshot(CommonRoomFields):
    game_type: Literal["2048"] = "2048"
    mode: Literal["1v1"] = "1v1"
    participants: tuple[Game2048Participant, ...]
    boards: tuple[Game2048BoardSnapshot, ...] | None
    winner_seat: Annotated[int, Field(ge=0, le=1)] | None
    draw: bool

    @field_validator("participants", mode="before")
    @classmethod
    def freeze_participants(cls, value: object) -> object:
        return _freeze_sequence(value)

    @field_validator("boards", mode="before")
    @classmethod
    def freeze_boards(cls, value: object) -> object:
        return _freeze_sequence(value)

    @field_validator("boards")
    @classmethod
    def validate_boards(
        cls, boards: tuple[Game2048BoardSnapshot, ...] | None
    ) -> tuple[Game2048BoardSnapshot, ...] | None:
        if boards is None:
            return None
        if len(boards) != 2 or {board.seat for board in boards} != {0, 1}:
            raise ValueError("2048 snapshots require boards for seats 0 and 1")
        return boards


RoomSnapshot = Game2048Snapshot


class ServerSnapshotMessage(_CorrelatedModel):
    type: Literal["snapshot"] = "snapshot"
    snapshot: RoomSnapshot


class ServerErrorMessage(_CorrelatedModel):
    type: Literal["error"] = "error"
    code: RoomErrorCode
    message: str = Field(min_length=1)
    current_version: SafeInteger
    current_board_version: SafeInteger | None = None

    @model_serializer(mode="wrap")
    def omit_absent_error_fields(
        self, handler: SerializerFunctionWrapHandler
    ) -> dict[str, object]:
        serialized = cast(dict[str, object], handler(self))
        if self.request_id is None:
            serialized.pop("request_id", None)
        if self.current_board_version is None:
            serialized.pop("current_board_version", None)
        return serialized


type ServerMessage = ServerSnapshotMessage | ServerErrorMessage
