"""CORS/preflight handling for the non-room JSON API surface.

Mirrors the origin policy already enforced for the room HTTP surface
(``entry.py``'s ``_room_origin_policy``) so browser-origin callers of
``/v1/me``, ``/v1/game-event-chunks``, ``/v1/game-results``, and
``/v1/high-scores/{game_type}`` get the same treatment: same-origin/
no-``Origin`` requests (the Node/CLI client) pass through untouched,
allow-listed browser origins get ``Access-Control-Allow-Origin`` and
working preflight, and any other origin is rejected before it reaches
the route handler.
"""

from typing import Literal

from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import Response
from starlette.types import ASGIApp, Message, Receive, Scope, Send

from ..auth import request_environment
from ..bindings import value_field
from ..multiplayer.protocol import canonical_http_origin

_ROUTE_METHODS: dict[str, Literal["GET", "POST"]] = {
    "/v1/me": "GET",
    "/v1/game-event-chunks": "POST",
    "/v1/game-results": "POST",
}
_LEADERBOARD_PREFIX = "/v1/high-scores/"


def _allowed_method(path: str) -> Literal["GET", "POST"] | None:
    method = _ROUTE_METHODS.get(path)
    if method is not None:
        return method
    if path.startswith(_LEADERBOARD_PREFIX) and len(path) > len(_LEADERBOARD_PREFIX):
        return "GET"
    return None


def _origin_policy(
    request: Request,
) -> tuple[Literal["allowed", "rejected", "unavailable"], str | None]:
    origins = request.headers.getlist("origin")
    if not origins:
        return "allowed", None
    if len(origins) != 1 or "," in origins[0]:
        return "rejected", None

    origin = origins[0]
    try:
        configured = value_field(request_environment(request), "ALLOWED_GAME_ORIGINS")
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


def _cors_headers(origin: str | None) -> dict[str, str]:
    headers = {"Vary": "Origin"}
    if origin is not None:
        headers["Access-Control-Allow-Origin"] = origin
    return headers


class ApiCorsMiddleware:
    """Pure-ASGI middleware guarding the non-room JSON API surface."""

    def __init__(self, app: ASGIApp) -> None:
        self._app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self._app(scope, receive, send)
            return

        request = Request(scope, receive=receive)
        allowed_method = _allowed_method(request.url.path)
        if allowed_method is None:
            await self._app(scope, receive, send)
            return

        policy, origin = _origin_policy(request)
        if policy == "unavailable":
            response = Response(status_code=503)
            await response(scope, receive, send)
            return
        if policy == "rejected":
            response = Response(status_code=403)
            await response(scope, receive, send)
            return

        if request.method == "OPTIONS":
            headers = _cors_headers(origin)
            headers["Access-Control-Allow-Methods"] = allowed_method
            headers["Access-Control-Allow-Headers"] = "Authorization, Content-Type"
            response = Response(status_code=204, headers=headers)
            await response(scope, receive, send)
            return

        if origin is None:
            await self._app(scope, receive, send)
            return

        cors_headers = _cors_headers(origin)

        async def send_with_cors_headers(message: Message) -> None:
            if message["type"] == "http.response.start":
                headers = list(message.get("headers", []))
                for key, value in cors_headers.items():
                    headers.append((key.encode("latin-1"), value.encode("latin-1")))
                message = {**message, "headers": headers}
            await send(message)

        await self._app(scope, receive, send_with_cors_headers)


def install_api_cors(app: Starlette) -> None:
    app.add_middleware(ApiCorsMiddleware)
