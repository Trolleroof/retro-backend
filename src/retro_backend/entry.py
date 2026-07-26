"""Cloudflare Python Worker entrypoints for HTTP and Queue events."""

import sys
from http import HTTPMethod
from typing import Literal, Protocol, cast
from urllib.parse import urlsplit

import asgi
from fastapi import HTTPException
from fastapi import Request as FastAPIRequest
from workers import Blob, Request, Response, WorkerEntrypoint

from retro_backend.app import app
from retro_backend.auth import require_user_id
from retro_backend.auth_cache import (
    AuthCache,
    AuthCacheEnvironment,
    build_auth_cache_environment,
)
from retro_backend.bindings import value_field
from retro_backend.chunk_gate import ChunkGate
from retro_backend.consumers.dispatch import dispatch_queue
from retro_backend.multiplayer.errors import RoomFault
from retro_backend.multiplayer.protocol import canonical_http_origin, parse_room_id
from retro_backend.multiplayer.room import GameRoom
from retro_backend.worker_http import (
    fetch_with_body_limit,
    is_room_http_surface,
    request_header_items,
)
from retro_backend.worker_websocket import handle_room_socket

if sys.platform == "emscripten":
    from js import caches  # pyright: ignore[reportMissingImports]
else:
    caches = None

__all__ = ["ChunkGate", "GameRoom", "Default"]


class _WorkerJsRequest(Protocol):
    url: str
    method: str
    headers: object
    body: object
    redirect: str


class _AuthHeaders:
    __slots__ = ("_items",)

    def __init__(self, items: list[tuple[str, str]]) -> None:
        self._items = items

    def get(self, name: str, default: str | None = None) -> str | None:
        values = [
            value
            for key, value in self._items
            if key.casefold() == name.casefold()
        ]
        return ", ".join(values) if values else default


class _AuthRequest:
    __slots__ = ("headers", "scope", "url")

    def __init__(
        self,
        headers: _AuthHeaders,
        environment: object,
        url: str,
    ) -> None:
        self.headers = headers
        self.scope = {"env": environment}
        self.url = url


def _rebuild_worker_request(
    request: object,
    body: bytes,
    headers: list[tuple[str, str]],
) -> _WorkerJsRequest:
    raw_request = getattr(request, "js_object", request)
    incoming = cast(_WorkerJsRequest, raw_request)
    bounded_request = Request(
        incoming.url,
        method=cast(HTTPMethod, incoming.method),
        headers=headers,
        body=Blob(body).js_object,
        redirect=incoming.redirect,
    )
    return bounded_request.js_object


def _too_large_response() -> Response:
    return Response.from_json(
        {"detail": "request body exceeds maximum size"},
        status=413,
    )


def _room_cors_headers(origin: str | None) -> dict[str, str]:
    headers = {"Vary": "Origin"}
    if origin is not None:
        headers["Access-Control-Allow-Origin"] = origin
    return headers


def _room_json_response(
    payload: dict[str, object],
    *,
    status: int,
    origin: str | None = None,
    headers: dict[str, str] | None = None,
) -> Response:
    response_headers = _room_cors_headers(origin)
    response_headers.update(headers or {})
    return Response.from_json(
        payload,
        status=status,
        headers=response_headers,
    )


def _room_preflight_response(
    allowed_method: Literal["GET", "POST"],
    origin: str | None,
) -> Response:
    headers = _room_cors_headers(origin)
    headers.update(
        {
            "Access-Control-Allow-Methods": allowed_method,
            "Access-Control-Allow-Headers": "Authorization, Content-Type",
        }
    )
    return Response(None, status=204, headers=headers)


def _room_origin_policy(
    request: object,
    environment: object,
) -> tuple[Literal["allowed", "rejected", "unavailable"], str | None]:
    try:
        origins = [
            value
            for key, value in request_header_items(request)
            if key.casefold() == "origin"
        ]
    except Exception:
        return "unavailable", None
    if not origins:
        return "allowed", None
    if len(origins) != 1 or "," in origins[0]:
        return "rejected", None

    origin = origins[0]
    try:
        configured = value_field(environment, "ALLOWED_GAME_ORIGINS")
    except Exception:
        return "unavailable", None
    if not isinstance(configured, str):
        return "rejected", None
    allowed_origins = {
        canonical
        for allowed in configured.split(",")
        if (canonical := canonical_http_origin(allowed)) is not None
    }
    canonical_origin = canonical_http_origin(origin)
    if canonical_origin is None or canonical_origin not in allowed_origins:
        return "rejected", None
    return "allowed", origin


def _room_auth_request(request: object, environment: object) -> FastAPIRequest:
    raw_request = getattr(request, "js_object", request)
    url = value_field(raw_request, "url")
    if not isinstance(url, str):
        raise TypeError("Worker request URL is unavailable")
    return cast(
        FastAPIRequest,
        cast(
            object,
            _AuthRequest(
                _AuthHeaders(request_header_items(request)),
                environment,
                url,
            ),
        ),
    )


async def _room_auth_response(
    request: object,
    environment: object,
    origin: str | None,
) -> Response | None:
    try:
        user_id = await require_user_id(_room_auth_request(request, environment))
    except HTTPException as error:
        if error.status_code in {401, 403}:
            return _room_json_response(
                {
                    "error": {
                        "code": "unauthorized",
                        "message": "Authentication required",
                    }
                },
                status=401,
                origin=origin,
                headers={"WWW-Authenticate": "Bearer"},
            )
        return _room_json_response(
            {
                "error": {
                    "code": "service_unavailable",
                    "message": "Authentication service unavailable",
                }
            },
            status=503,
            origin=origin,
        )
    except Exception:
        return _room_json_response(
            {
                "error": {
                    "code": "service_unavailable",
                    "message": "Authentication service unavailable",
                }
            },
            status=503,
            origin=origin,
        )
    if type(user_id) is not str or not user_id:
        return _room_json_response(
            {
                "error": {
                    "code": "unauthorized",
                    "message": "Authentication required",
                }
            },
            status=401,
            origin=origin,
            headers={"WWW-Authenticate": "Bearer"},
        )
    return None


async def _room_policy_response(
    request: object,
    environment: object,
    *,
    allowed_method: Literal["GET", "POST"] | None,
    invalid_request: bool,
) -> Response | None:
    policy, origin = _room_origin_policy(request, environment)
    if policy == "unavailable":
        return _room_json_response(
            {
                "error": {
                    "code": "service_unavailable",
                    "message": "Room service unavailable",
                }
            },
            status=503,
        )
    if policy == "rejected":
        return _room_json_response(
            {"error": {"code": "unauthorized", "message": "Unauthorized"}},
            status=403,
        )
    raw_request = getattr(request, "js_object", request)
    method = value_field(raw_request, "method")
    if invalid_request:
        if method == "OPTIONS" and origin is None:
            return _room_json_response(
                {"error": {"code": "unauthorized", "message": "Unauthorized"}},
                status=403,
            )
        if allowed_method is None:
            return _room_json_response(
                {
                    "error": {
                        "code": "invalid_request",
                        "message": "Room route not found",
                    }
                },
                status=404,
                origin=origin,
            )
        url = value_field(raw_request, "url")
        path = urlsplit(url).path if isinstance(url, str) else ""
        message = (
            "Invalid room creation request"
            if path == "/v1/rooms"
            else "Invalid request"
        )
        return _room_json_response(
            {"error": {"code": "invalid_request", "message": message}},
            status=400,
            origin=origin,
        )
    if allowed_method is None:
        return None

    if method == "OPTIONS":
        if origin is None:
            return _room_json_response(
                {"error": {"code": "unauthorized", "message": "Unauthorized"}},
                status=403,
            )
        return _room_preflight_response(allowed_method, origin)
    if method != allowed_method:
        return _room_json_response(
            {"error": {"code": "invalid_request", "message": "Method not allowed"}},
            status=405,
            origin=origin,
            headers={"Allow": allowed_method},
        )

    auth_response = await _room_auth_response(request, environment, origin)
    if auth_response is not None:
        return auth_response

    path = urlsplit(str(value_field(raw_request, "url", ""))).path
    if path != "/v1/rooms":
        room_id = path.removeprefix("/v1/rooms/").split("/", 1)[0]
        try:
            parse_room_id(room_id)
        except RoomFault:
            return _room_json_response(
                {"error": {"code": "room_not_found", "message": "Room not found"}},
                status=404,
                origin=origin,
            )
    return None


def _cache_request(cache_url: str) -> object:
    return Request(cache_url, method=HTTPMethod.GET).js_object


def _cache_response(body: str, ttl_seconds: int) -> object:
    return Response(
        body,
        headers={
            "Cache-Control": f"max-age={ttl_seconds}",
            "Content-Type": "application/json",
        },
    ).js_object


def _environment_with_auth_cache(environment: object) -> object:
    return build_auth_cache_environment(
        environment,
        caches,
        request_factory=_cache_request,
        response_factory=_cache_response,
    )


def _environment_with_request_auth_cache(environment: object) -> object:
    cached_environment = environment
    if isinstance(cached_environment, AuthCacheEnvironment):
        return cached_environment
    existing_cache = value_field(cached_environment, "AUTH_CACHE")
    return AuthCacheEnvironment(
        cached_environment,
        cast(AuthCache, existing_cache) if existing_cache is not None else None,
    )


class Default(WorkerEntrypoint):
    async def fetch(self, request: object) -> object:
        environment = _environment_with_auth_cache(self.env)
        socket_response = await handle_room_socket(request, environment)
        if socket_response is not None:
            return socket_response
        if is_room_http_surface(request):
            environment = _environment_with_request_auth_cache(environment)
        return await fetch_with_body_limit(
            request,
            environment,
            app=app,
            asgi_fetch=asgi.fetch,
            request_factory=_rebuild_worker_request,
            too_large_response=_too_large_response,
            room_response_factory=_room_policy_response,
        )

    async def queue(self, batch: object, *_runtime_args: object) -> None:
        """Consume one gameplay queue batch.

        The Workers Python runtime invokes the handler as
        ``queue(self, batch, env, ctx)``. Bindings are already reachable through
        ``self.env``, so the trailing arguments are accepted and ignored; without
        the catch-all every batch raises ``TypeError`` and retries into the DLQ.
        """
        await dispatch_queue(batch, self.env)
