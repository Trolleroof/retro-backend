"""Route multiplayer room WebSocket upgrades before the ASGI adapter."""

import json
import string
import sys
from collections.abc import Mapping
from typing import Protocol, cast
from urllib.parse import urlsplit

from .bindings import value_field
from .multiplayer.errors import RoomErrorCode, RoomFault, error_envelope
from .multiplayer.protocol import canonical_http_origin, parse_room_id
from .multiplayer.tickets import validate_ticket
from .worker_http import _header_items  # pyright: ignore[reportPrivateUsage]

if sys.platform == "emscripten":
    from workers import Response  # pyright: ignore[reportAssignmentType]
else:

    class Response:
        """Minimal CPython response preserving the Workers constructor shape."""

        def __init__(
            self,
            body: object | None = None,
            status: int = 200,
            status_text: str = "",
            headers: Mapping[str, str] | list[tuple[str, str]] | None = None,
            web_socket: object | None = None,
        ) -> None:
            self.body = body
            self.status = status
            self.status_text = status_text
            self.web_socket = web_socket
            header_items = headers.items() if isinstance(headers, Mapping) else headers or []
            self.headers = {
                key.casefold(): value for key, value in header_items
            }


MAX_SOCKET_COMPONENT_BYTES = 1024
MAX_SOCKET_QUERY_BYTES = 1024

_HEX_DIGITS = frozenset(string.hexdigits)
_SOCKET_PREFIX_PARTS = ["", "v1", "rooms"]
_DOUBLE_ENCODED_DELIMITERS = {
    "%2F": "/",
    "%2f": "/",
    "%5C": "\\",
    "%5c": "\\",
}


class _ComponentTooLarge(ValueError):
    pass


class _OriginRejected(ValueError):
    pass


class _RoomStub(Protocol):
    async def fetch(self, request: object) -> object: ...


class _RoomNamespace(Protocol):
    def getByName(self, room_id: str) -> _RoomStub: ...


def _is_socket_candidate(parts: list[str]) -> bool:
    return (
        len(parts) == 5
        and parts[:3] == _SOCKET_PREFIX_PARTS
        and parts[4] == "socket"
    )


def _decode_component(value: str, *, plus_as_space: bool = False) -> str:
    decoded = bytearray()
    index = 0
    while index < len(value):
        character = value[index]
        if character == "%":
            if (
                index + 2 >= len(value)
                or value[index + 1] not in _HEX_DIGITS
                or value[index + 2] not in _HEX_DIGITS
            ):
                raise ValueError("invalid percent escape")
            decoded.append(int(value[index + 1 : index + 3], 16))
            index += 3
        else:
            if plus_as_space and character == "+":
                character = " "
            decoded.extend(character.encode("utf-8"))
            index += 1
        if len(decoded) > MAX_SOCKET_COMPONENT_BYTES:
            raise _ComponentTooLarge

    try:
        return bytes(decoded).decode("utf-8")
    except UnicodeDecodeError as error:
        raise ValueError("component is not UTF-8") from error


def _decode_path_segments(path: str) -> list[str]:
    raw_segments = path.split("/")
    slash_bytes = len(raw_segments) - 1
    if slash_bytes > MAX_SOCKET_COMPONENT_BYTES:
        raise _ComponentTooLarge

    decoded_segments: list[str] = []
    decoded_bytes = slash_bytes
    for raw_segment in raw_segments:
        segment = _decode_component(raw_segment)
        decoded_bytes += len(segment.encode("utf-8"))
        if decoded_bytes > MAX_SOCKET_COMPONENT_BYTES:
            raise _ComponentTooLarge
        decoded_segments.append(segment)
    return decoded_segments


def _delimiter_alias(segment: str) -> tuple[str, bool]:
    alias = segment
    for encoded, delimiter in _DOUBLE_ENCODED_DELIMITERS.items():
        alias = alias.replace(encoded, delimiter)
    return alias, alias != segment or "/" in alias or "\\" in alias


def _is_encoded_socket_alias(decoded_segments: list[str]) -> bool:
    alias_segments: list[str] = []
    encoded_delimiters: list[bool] = []
    for segment in decoded_segments:
        alias, has_delimiter = _delimiter_alias(segment)
        alias_segments.append(alias)
        encoded_delimiters.append(has_delimiter)
    if not any(encoded_delimiters):
        return False

    expanded = "/".join(alias_segments).replace("\\", "/").split("/")
    if _is_socket_candidate(expanded):
        return True

    if len(alias_segments) != 5 or alias_segments[0] != "":
        return False
    for index, expected in ((1, "v1"), (2, "rooms"), (4, "socket")):
        segment = alias_segments[index]
        if encoded_delimiters[index]:
            segment = segment.replace("/", "").replace("\\", "")
        if segment != expected:
            return False
    return any(encoded_delimiters[1:])


def _header_values(headers: list[tuple[str, str]], name: str) -> list[str]:
    normalized_name = name.casefold()
    return [value for key, value in headers if key.casefold() == normalized_name]


def _allowed_origin(
    headers: list[tuple[str, str]],
    environment: object,
) -> str | None:
    origins = _header_values(headers, "origin")
    if not origins:
        return None
    if len(origins) != 1:
        raise _OriginRejected

    configured = value_field(environment, "ALLOWED_GAME_ORIGINS")
    if not isinstance(configured, str):
        raise _OriginRejected
    allowed = {
        canonical
        for value in configured.split(",")
        if (canonical := canonical_http_origin(value)) is not None
    }
    origin = origins[0]
    canonical_origin = canonical_http_origin(origin)
    if canonical_origin is None or canonical_origin not in allowed:
        raise _OriginRejected
    return origin


def _fault_response(
    code: RoomErrorCode,
    *,
    status: int,
    message: str | None = None,
    origin: str | None = None,
    headers: Mapping[str, str] | None = None,
) -> Response:
    response_headers = {
        "content-type": "application/json",
        "vary": "Origin",
    }
    if origin is not None:
        response_headers["access-control-allow-origin"] = origin
    response_headers.update(headers or {})
    return Response(
        json.dumps(
            error_envelope(
                RoomFault(code, message) if message is not None else RoomFault.for_code(code)
            ),
            ensure_ascii=True,
            allow_nan=False,
            separators=(",", ":"),
        ),
        status=status,
        headers=response_headers,
    )


def _invalid_ticket(origin: str | None) -> Response:
    return _fault_response("invalid_ticket", status=401, origin=origin)


def _preflight_response(origin: str) -> Response:
    return Response(
        None,
        status=204,
        headers={
            "access-control-allow-origin": origin,
            "access-control-allow-methods": "GET",
            "access-control-allow-headers": "Authorization, Content-Type",
            "vary": "Origin",
        },
    )


def _with_allowed_origin(response: object, origin: str | None) -> object:
    if origin is None:
        return response

    raw_response = getattr(response, "js_object", response)
    body = value_field(raw_response, "body", value_field(response, "body"))
    status = value_field(response, "status", value_field(raw_response, "status"))
    if type(status) is not int:
        raise TypeError("response status must be an integer")
    status_text = value_field(response, "status_text")
    if not isinstance(status_text, str):
        status_text = value_field(raw_response, "statusText", "")
    if not isinstance(status_text, str):
        raise TypeError("response status text must be a string")
    web_socket = value_field(response, "web_socket")
    if web_socket is None:
        web_socket = value_field(raw_response, "webSocket")

    raw_headers = value_field(response, "headers", value_field(raw_response, "headers", ()))
    headers = [
        (key, value)
        for key, value in _header_items(() if raw_headers is None else raw_headers)
        if key.casefold() != "access-control-allow-origin"
    ]
    vary_indexes = [
        index for index, (key, _value) in enumerate(headers) if key.casefold() == "vary"
    ]
    vary_values = [
        item.strip().casefold()
        for index in vary_indexes
        for item in headers[index][1].split(",")
    ]
    if "origin" not in vary_values:
        if vary_indexes:
            index = vary_indexes[0]
            key, value = headers[index]
            headers[index] = (key, f"{value}, Origin" if value else "Origin")
        else:
            headers.append(("Vary", "Origin"))
    headers.append(("Access-Control-Allow-Origin", origin))

    return Response(
        body,
        status=status,
        status_text=status_text,
        headers=headers,
        web_socket=web_socket,
    )


def _ticket_from_query(query: str, *, has_fragment: bool) -> str:
    if has_fragment or not query or "&" in query:
        raise ValueError("query must contain one field")
    raw_key, separator, raw_value = query.partition("=")
    if not separator:
        raise ValueError("query field requires a value")
    key = _decode_component(raw_key, plus_as_space=True)
    ticket = _decode_component(raw_value, plus_as_space=True)
    if key != "ticket" or not ticket:
        raise ValueError("query must contain one ticket")
    return ticket


async def handle_room_socket(request: object, environment: object) -> object | None:
    """Validate and forward one exact room socket route before ASGI handling."""
    raw_request = getattr(request, "js_object", request)
    try:
        raw_url = value_field(raw_request, "url")
    except Exception:
        return None
    if not isinstance(raw_url, str):
        return None
    try:
        parsed = urlsplit(raw_url)
    except (TypeError, ValueError):
        return None

    raw_segments = parsed.path.split("/")
    raw_candidate = _is_socket_candidate(raw_segments)
    path_segments: list[str] = []
    path_fault: tuple[RoomErrorCode, int] | None = None
    decoded_candidate = False
    try:
        path_segments = _decode_path_segments(parsed.path)
    except _ComponentTooLarge:
        if not raw_candidate:
            return None
        path_fault = ("invalid_request", 414)
    except (UnicodeError, ValueError):
        if not raw_candidate:
            return None
        path_fault = ("room_not_found", 404)
    else:
        encoded_alias = _is_encoded_socket_alias(path_segments)
        decoded_candidate = _is_socket_candidate(path_segments)
        if not raw_candidate and not decoded_candidate and not encoded_alias:
            return None
        if encoded_alias or not decoded_candidate:
            path_fault = ("room_not_found", 404)

    try:
        raw_headers = value_field(raw_request, "headers", ())
        headers = _header_items(() if raw_headers is None else raw_headers)
    except Exception:
        return _fault_response("service_unavailable", status=503)

    try:
        origin = _allowed_origin(headers, environment)
    except _OriginRejected:
        return _fault_response("unauthorized", status=403)
    except Exception:
        return _fault_response("service_unavailable", status=503)

    if path_fault is not None:
        code, status = path_fault
        return _fault_response(code, status=status, origin=origin)

    try:
        method = value_field(raw_request, "method")
    except Exception:
        method = None
    if method == "OPTIONS":
        if origin is None:
            return _fault_response("unauthorized", status=403)
        return _preflight_response(origin)
    if method != "GET":
        return _fault_response(
            "invalid_request",
            status=405,
            message="Method not allowed",
            origin=origin,
            headers={"allow": "GET"},
        )

    room_id = path_segments[3]
    try:
        room_id = parse_room_id(room_id)
    except RoomFault:
        return _fault_response("room_not_found", status=404, origin=origin)

    upgrades = _header_values(headers, "upgrade")
    if len(upgrades) != 1 or upgrades[0].casefold() != "websocket":
        return _fault_response(
            "invalid_request",
            status=426,
            message="WebSocket upgrade required",
            origin=origin,
            headers={"upgrade": "websocket"},
        )

    raw_search = f"?{parsed.query}" if "?" in raw_url.partition("#")[0] else ""
    if len(raw_search.encode("utf-8")) > MAX_SOCKET_QUERY_BYTES:
        return _invalid_ticket(origin)

    query_error = False
    try:
        _decode_component(parsed.query, plus_as_space=True)
    except _ComponentTooLarge:
        return _invalid_ticket(origin)
    except (UnicodeError, ValueError):
        query_error = True

    if query_error:
        return _invalid_ticket(origin)
    try:
        ticket = _ticket_from_query(parsed.query, has_fragment="#" in raw_url)
        validate_ticket(ticket)
    except RoomFault:
        return _invalid_ticket(origin)
    except (UnicodeError, ValueError):
        return _invalid_ticket(origin)
    except Exception:
        return _fault_response("service_unavailable", status=503, origin=origin)

    try:
        namespace = cast(_RoomNamespace, value_field(environment, "GAME_ROOMS"))
        stub = namespace.getByName(room_id)
        response = await stub.fetch(raw_request)
        return _with_allowed_origin(response, origin)
    except Exception:
        return _fault_response("service_unavailable", status=503, origin=origin)


__all__ = ["handle_room_socket"]
