"""Authenticated JSON routes for named multiplayer rooms."""

import json
import time
from dataclasses import dataclass
from typing import Literal, Never, Protocol, cast, override
from urllib.parse import unquote, urlsplit, urlunsplit
from uuid import uuid4

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import JSONResponse, Response
from fastapi.routing import APIRoute
from pydantic import ValidationError
from starlette.routing import Match
from starlette.types import Receive, Scope, Send

from ..auth import require_user_id
from ..multiplayer.client import RoomClient
from ..multiplayer.errors import (
    RoomFault,
    error_envelope,
    http_status_for_fault,
)
from ..multiplayer.protocol import (
    MAX_SAFE_INTEGER,
    GameDescriptor,
    canonical_http_origin,
    parse_room_id,
)
from .request_body import BodyTooLarge, read_limited_body

MAX_CREATE_ROOM_BODY_BYTES = 4 * 1024
MAX_ROOM_PATH_BYTES = 1024

_ROOMS_PATH = "/v1/rooms"
_DISPATCH_METHODS = ["GET", "OPTIONS", "POST"]

type _RouteKind = Literal["collection", "member", "join", "tickets", "socket"]


class _RoomEnvironment(Protocol):
    ALLOWED_GAME_ORIGINS: object
    PUBLIC_GAME_URL: object


@dataclass(frozen=True, slots=True)
class _MatchedRoute:
    kind: _RouteKind
    allowed_method: Literal["GET", "POST"]
    room_id: str | None = None


class _RouteFailure(Exception):
    def __init__(
        self,
        fault: RoomFault,
        *,
        status_code: int | None = None,
        headers: dict[str, str] | None = None,
    ) -> None:
        super().__init__(fault.code)
        self.fault = fault
        self.status_code = status_code
        self.headers = dict(headers or {})


class _RoomAPIRoute(APIRoute):
    """Dispatch every HTTP method through the room surface policy."""

    @override
    def matches(self, scope: Scope) -> tuple[Match, Scope]:
        match, child_scope = super().matches(scope)
        if scope["type"] == "http" and match == Match.PARTIAL:
            return Match.FULL, child_scope
        return match, child_scope

    @override
    async def handle(self, scope: Scope, receive: Receive, send: Send) -> None:
        await self.app(scope, receive, send)


router = APIRouter(route_class=_RoomAPIRoute)


def _fail(
    code: Literal[
        "unauthorized",
        "service_unavailable",
        "invalid_request",
        "room_not_found",
    ],
    *,
    message: str | None = None,
    status_code: int | None = None,
    headers: dict[str, str] | None = None,
) -> Never:
    raise _RouteFailure(
        RoomFault(code, message) if message is not None else RoomFault.for_code(code),
        status_code=status_code,
        headers=headers,
    )


def _match_route(request: Request) -> _MatchedRoute:
    path = request.scope.get("path")
    if not isinstance(path, str):
        _fail(
            "invalid_request",
            message="Invalid request path",
            status_code=400,
        )
    try:
        path_size = len(path.encode("utf-8"))
    except UnicodeError:
        _fail(
            "invalid_request",
            message="Invalid request path",
            status_code=400,
        )
    if path_size > MAX_ROOM_PATH_BYTES:
        _fail(
            "invalid_request",
            message="Request path is too long",
            status_code=414,
        )

    if path == _ROOMS_PATH:
        return _MatchedRoute("collection", "POST")
    prefix = f"{_ROOMS_PATH}/"
    if not path.startswith(prefix):
        _fail(
            "invalid_request",
            message="Room route not found",
            status_code=404,
        )

    parts = path[len(prefix) :].split("/")
    if len(parts) == 1 and parts[0]:
        return _MatchedRoute("member", "GET", parts[0])
    if len(parts) == 2 and parts[0] and parts[1] in {"join", "tickets", "socket"}:
        kind = cast(Literal["join", "tickets", "socket"], parts[1])
        method = "GET" if kind == "socket" else "POST"
        return _MatchedRoute(kind, method, parts[0])
    _fail(
        "invalid_request",
        message="Room route not found",
        status_code=404,
    )


def _scope_environment(request: Request) -> _RoomEnvironment:
    try:
        return cast(_RoomEnvironment, request.scope["env"])
    except Exception as error:
        raise RoomFault.service_unavailable() from error


def _validate_origin(request: Request) -> str | None:
    origins = request.headers.getlist("origin")
    if not origins:
        return None
    if len(origins) != 1:
        _fail("unauthorized", status_code=403)
    origin = origins[0]

    environment = _scope_environment(request)
    try:
        configured = environment.ALLOWED_GAME_ORIGINS
    except AttributeError:
        _fail("unauthorized", status_code=403)
    except Exception as error:
        raise RoomFault.service_unavailable() from error
    if not isinstance(configured, str):
        _fail("unauthorized", status_code=403)
    canonical_origin = canonical_http_origin(origin)
    allowed_origins = {
        canonical
        for configured_origin in configured.split(",")
        if (canonical := canonical_http_origin(configured_origin.strip())) is not None
    }
    if canonical_origin is None or canonical_origin not in allowed_origins:
        _fail("unauthorized", status_code=403)
    return origin


def _cors_headers(origin: str | None) -> dict[str, str]:
    headers = {"Vary": "Origin"}
    if origin is not None:
        headers["Access-Control-Allow-Origin"] = origin
    return headers


def _json_response(
    payload: dict[str, object],
    *,
    status_code: int,
    origin: str | None,
    headers: dict[str, str] | None = None,
) -> JSONResponse:
    response_headers = _cors_headers(origin)
    response_headers.update(headers or {})
    return JSONResponse(
        content=payload,
        status_code=status_code,
        headers=response_headers,
    )


def _fault_response(
    fault: RoomFault,
    *,
    origin: str | None,
    status_code: int | None = None,
    headers: dict[str, str] | None = None,
) -> JSONResponse:
    return _json_response(
        error_envelope(fault),
        status_code=status_code or http_status_for_fault(fault),
        origin=origin,
        headers=headers,
    )


def _preflight(route: _MatchedRoute, origin: str | None) -> Response:
    headers = _cors_headers(origin)
    headers.update(
        {
            "Access-Control-Allow-Methods": route.allowed_method,
            "Access-Control-Allow-Headers": "Authorization, Content-Type",
        }
    )
    return Response(status_code=204, headers=headers)


def _reject_json_constant(_constant: str) -> Never:
    raise ValueError("non-standard JSON constant")


def _exact_object(pairs: list[tuple[str, object]]) -> dict[str, object]:
    result: dict[str, object] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("duplicate JSON key")
        result[key] = value
    return result


def _valid_json_content_type(request: Request) -> bool:
    content_types = request.headers.getlist("content-type")
    if len(content_types) != 1:
        return False
    content_type = content_types[0]
    if "," in content_type:
        return False
    parts = [part.strip() for part in content_type.split(";")]
    if parts[0].casefold() != "application/json":
        return False
    if len(parts) == 1:
        return True
    return len(parts) == 2 and parts[1].casefold() in {
        "charset=utf-8",
        'charset="utf-8"',
    }


async def _parse_create_body(request: Request) -> GameDescriptor:
    if not _valid_json_content_type(request):
        _fail("invalid_request", message="Invalid room creation request")
    try:
        raw_body = await read_limited_body(
            request,
            max_bytes=MAX_CREATE_ROOM_BODY_BYTES,
        )
        if not raw_body:
            _fail("invalid_request", message="Invalid room creation request")
        decoded = raw_body.decode("utf-8")
        payload = cast(
            object,
            json.loads(
                decoded,
                parse_constant=_reject_json_constant,
                object_pairs_hook=_exact_object,
            ),
        )
        return GameDescriptor.model_validate(payload)
    except _RouteFailure:
        raise
    except (BodyTooLarge, UnicodeError, ValueError, TypeError, ValidationError):
        _fail("invalid_request", message="Invalid room creation request")


async def _authenticated_user_id(request: Request) -> str:
    try:
        user_id = await require_user_id(request)
    except HTTPException as error:
        if error.status_code in {401, 403}:
            raise _RouteFailure(
                RoomFault("unauthorized", "Authentication required"),
                status_code=401,
                headers={"WWW-Authenticate": "Bearer"},
            ) from error
        raise _RouteFailure(
            RoomFault(
                "service_unavailable",
                "Authentication service unavailable",
            ),
            status_code=503,
        ) from error
    except Exception as error:
        raise _RouteFailure(
            RoomFault(
                "service_unavailable",
                "Authentication service unavailable",
            ),
            status_code=503,
        ) from error
    if type(user_id) is not str or not user_id:
        raise _RouteFailure(
            RoomFault("unauthorized", "Authentication required"),
            status_code=401,
            headers={"WWW-Authenticate": "Bearer"},
        )
    return user_id


def _safe_epoch_ms(value: object) -> int:
    if type(value) is not int or not 0 <= value <= MAX_SAFE_INTEGER:
        raise RoomFault.service_unavailable()
    return value


def _now_ms() -> int:
    return time.time_ns() // 1_000_000


def _public_game_url(request: Request) -> str:
    environment = _scope_environment(request)
    try:
        configured = environment.PUBLIC_GAME_URL
    except Exception as error:
        raise RoomFault.service_unavailable() from error
    if (
        not isinstance(configured, str)
        or not configured
        or configured != configured.strip()
        or "\\" in configured
        or any(character.isspace() or ord(character) < 32 for character in configured)
    ):
        raise RoomFault.service_unavailable()
    try:
        parsed = urlsplit(configured)
        hostname = parsed.hostname
        port = parsed.port
    except (TypeError, ValueError) as error:
        raise RoomFault.service_unavailable() from error
    if (
        parsed.scheme not in {"http", "https"}
        or not parsed.netloc
        or hostname is None
        or parsed.username is not None
        or parsed.password is not None
        or parsed.query
        or parsed.fragment
        or port == 0
    ):
        raise RoomFault.service_unavailable()

    for segment in parsed.path.split("/"):
        decoded_segment = unquote(segment)
        if (
            decoded_segment in {".", ".."}
            or "/" in decoded_segment
            or "\\" in decoded_segment
            or any(ord(character) < 32 for character in decoded_segment)
        ):
            raise RoomFault.service_unavailable()
    return configured


def _invite_url(public_game_url: str, room_id: str) -> str:
    parsed = urlsplit(public_game_url)
    base_path = parsed.path.rstrip("/")
    invite_path = f"{base_path}/join/{room_id}"
    return urlunsplit((parsed.scheme, parsed.netloc, invite_path, "", ""))


def _canonical_uuid4() -> str:
    generated = uuid4()
    if generated.version != 4:
        raise RoomFault.service_unavailable()
    room_id = str(generated)
    if parse_room_id(room_id) != room_id:
        raise RoomFault.service_unavailable()
    return room_id


async def _create_room(
    request: Request,
    user_id: str,
    descriptor: GameDescriptor,
    origin: str | None,
) -> JSONResponse:
    now_ms = _safe_epoch_ms(_now_ms())
    room_id = _canonical_uuid4()
    public_game_url = _public_game_url(request)
    invite_url = _invite_url(public_game_url, room_id)
    client = RoomClient.from_request(request, room_id)
    snapshot = await client.create(room_id, user_id, now_ms, descriptor)
    serialized_snapshot = cast(dict[str, object], snapshot.model_dump(mode="json"))
    return _json_response(
        {
            "room_id": room_id,
            "invite_url": invite_url,
            "expires_at": serialized_snapshot["expires_at"],
            "snapshot": serialized_snapshot,
        },
        status_code=201,
        origin=origin,
    )


async def _get_room(
    request: Request,
    room_id: str,
    origin: str | None,
) -> JSONResponse:
    user_id = await _authenticated_user_id(request)
    client = RoomClient.from_request(request, room_id)
    snapshot = await client.get_snapshot(user_id)
    return _json_response(
        cast(dict[str, object], snapshot.model_dump(mode="json")),
        status_code=200,
        origin=origin,
    )


async def _join_room(
    request: Request,
    room_id: str,
    origin: str | None,
) -> JSONResponse:
    user_id = await _authenticated_user_id(request)
    now_ms = _safe_epoch_ms(_now_ms())
    client = RoomClient.from_request(request, room_id)
    snapshot = await client.join(user_id, now_ms)
    return _json_response(
        cast(dict[str, object], snapshot.model_dump(mode="json")),
        status_code=200,
        origin=origin,
    )


async def _issue_ticket(
    request: Request,
    room_id: str,
    origin: str | None,
) -> JSONResponse:
    user_id = await _authenticated_user_id(request)
    now_ms = _safe_epoch_ms(_now_ms())
    client = RoomClient.from_request(request, room_id)
    ticket = await client.issue_ticket(user_id, now_ms)
    return _json_response(
        cast(dict[str, object], ticket.model_dump(mode="json")),
        status_code=200,
        origin=origin,
    )


async def _dispatch_room_request(request: Request) -> Response:
    origin: str | None = None
    try:
        origin = _validate_origin(request)
        route = _match_route(request)
        if request.method == "OPTIONS":
            if origin is None:
                _fail("unauthorized", status_code=403)
            return _preflight(route, origin)
        if request.method != route.allowed_method:
            _fail(
                "invalid_request",
                message="Method not allowed",
                status_code=405,
                headers={"Allow": route.allowed_method},
            )

        if route.kind == "collection":
            user_id = await _authenticated_user_id(request)
            descriptor = await _parse_create_body(request)
            return await _create_room(request, user_id, descriptor, origin)
        if route.room_id is None:
            raise RoomFault.service_unavailable()
        room_id = parse_room_id(route.room_id)
        if route.kind == "socket":
            _fail(
                "invalid_request",
                message="Room route not found",
                status_code=404,
            )
        if route.kind == "member":
            return await _get_room(request, room_id, origin)
        if route.kind == "join":
            return await _join_room(request, room_id, origin)
        return await _issue_ticket(request, room_id, origin)
    except _RouteFailure as error:
        return _fault_response(
            error.fault,
            origin=origin,
            status_code=error.status_code,
            headers=error.headers,
        )
    except RoomFault as fault:
        return _fault_response(fault, origin=origin)
    except Exception:
        return _fault_response(RoomFault.service_unavailable(), origin=origin)


@router.api_route(_ROOMS_PATH, methods=_DISPATCH_METHODS, include_in_schema=False)
async def room_collection(request: Request) -> Response:
    return await _dispatch_room_request(request)


@router.api_route(
    f"{_ROOMS_PATH}/{{room_path:path}}",
    methods=_DISPATCH_METHODS,
    include_in_schema=False,
)
async def room_path(request: Request, room_path: str) -> Response:
    _ = room_path
    return await _dispatch_room_request(request)


__all__ = ["router"]
