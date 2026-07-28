import hashlib
from collections.abc import AsyncIterator
from dataclasses import dataclass, field
from datetime import UTC, datetime
from types import SimpleNamespace
from typing import Any, cast
from uuid import UUID

import pytest
from fastapi import HTTPException, Request
from httpx import ASGITransport, AsyncClient
from starlette.types import ASGIApp, Receive, Scope, Send

import retro_backend.api.rooms as rooms_module
from retro_backend.app import app
from retro_backend.multiplayer.client import RoomClient, RoomTicket
from retro_backend.multiplayer.protocol import MAX_SAFE_INTEGER, GameDescriptor

ROOM_ID = "123e4567-e89b-42d3-a456-426614174000"
NOW_MS = 1_774_027_200_000
CREATED_AT = "2026-03-20T12:00:00.000Z"
EXPIRES_AT = "2026-03-20T12:30:00.000Z"
VALID_TICKET = "A" * 43
ALLOWED_ORIGIN = "https://game.example"
CREATE_BODY = {"game_type": "2048", "mode": "1v1"}


@pytest.mark.parametrize(
    ("timestamp", "expected"),
    [
        (datetime(2026, 3, 20, 12, tzinfo=UTC), "2026-03-20T12:00:00.000Z"),
        (
            datetime(2026, 3, 20, 12, microsecond=123_999, tzinfo=UTC),
            "2026-03-20T12:00:00.123Z",
        ),
    ],
)
def test_room_ticket_serializes_timestamps_like_javascript_dates(
    timestamp: datetime,
    expected: str,
) -> None:
    ticket = RoomTicket.model_validate(
        {"ticket": VALID_TICKET, "expires_at": timestamp}
    )

    assert ticket.model_dump(mode="json")["expires_at"] == expected


def _snapshot(user_id: str = "usr_creator") -> dict[str, object]:
    return {
        "room_id": ROOM_ID,
        "status": "waiting",
        "created_at": CREATED_AT,
        "expires_at": EXPIRES_AT,
        "version": 0,
        "game_type": "2048",
        "mode": "1v1",
        "participants": [
            {
                "user_id": user_id,
                "seat": 0,
                "ready": False,
                "connected": False,
            }
        ],
        "boards": None,
        "winner_seat": None,
        "draw": False,
    }


def _success(value: object) -> dict[str, object]:
    return {"ok": True, "value": value}


def _failure(code: str, message: str) -> dict[str, object]:
    return {"ok": False, "error": {"code": code, "message": message}}


def _error(code: str, message: str) -> dict[str, object]:
    return {"error": {"code": code, "message": message}}


class FakeRoomStub:
    def __init__(self) -> None:
        self.create_result: object = _success(_snapshot())
        self.snapshot_result: object = _success(_snapshot())
        self.join_result: object = _success(_snapshot())
        self.ticket_result: object = _success(
            {"ticket": VALID_TICKET, "expires_at": EXPIRES_AT}
        )
        self.failure: Exception | None = None
        self.create_calls: list[object] = []
        self.snapshot_calls: list[object] = []
        self.join_calls: list[tuple[object, object]] = []
        self.ticket_calls: list[tuple[object, object]] = []

    def _raise_if_failed(self) -> None:
        if self.failure is not None:
            raise self.failure

    async def create_room_rpc(self, payload: object) -> object:
        self.create_calls.append(payload)
        self._raise_if_failed()
        return self.create_result

    async def get_snapshot_rpc(self, user_id: object) -> object:
        self.snapshot_calls.append(user_id)
        self._raise_if_failed()
        return self.snapshot_result

    async def join_rpc(self, user_id: object, now_ms: object) -> object:
        self.join_calls.append((user_id, now_ms))
        self._raise_if_failed()
        return self.join_result

    async def issue_ticket_rpc(self, user_id: object, now_ms: object) -> object:
        self.ticket_calls.append((user_id, now_ms))
        self._raise_if_failed()
        return self.ticket_result


class FakeRoomNamespace:
    def __init__(self, stub: FakeRoomStub) -> None:
        self.stub = stub
        self.get_by_name_calls: list[str] = []
        self.failure: Exception | None = None

    def getByName(self, room_id: str) -> FakeRoomStub:
        self.get_by_name_calls.append(room_id)
        if self.failure is not None:
            raise self.failure
        return self.stub


class _EnvironmentApp:
    def __init__(self, wrapped: ASGIApp, environment: object) -> None:
        self.wrapped = wrapped
        self.environment = environment

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        scope["env"] = self.environment
        await self.wrapped(scope, receive, send)


@dataclass(slots=True)
class AuthState:
    user_id: object = "usr_creator"
    failure: HTTPException | None = None
    calls: list[str] = field(default_factory=list)


@dataclass(slots=True)
class RoomAppHarness:
    http: AsyncClient
    environment: SimpleNamespace
    namespace: FakeRoomNamespace
    stub: FakeRoomStub
    auth: AuthState


def _request_for_namespace(namespace: FakeRoomNamespace) -> Request:
    scope = cast(
        Scope,
        {
            "type": "http",
            "method": "GET",
            "scheme": "https",
            "path": f"/v1/rooms/{ROOM_ID}",
            "raw_path": f"/v1/rooms/{ROOM_ID}".encode(),
            "query_string": b"",
            "headers": [],
            "client": ("127.0.0.1", 1234),
            "server": ("api.example", 443),
            "env": SimpleNamespace(GAME_ROOMS=namespace),
        },
    )
    return Request(scope)


@pytest.fixture
async def room_client(
    monkeypatch: pytest.MonkeyPatch,
) -> AsyncIterator[RoomAppHarness]:
    stub = FakeRoomStub()
    namespace = FakeRoomNamespace(stub)
    environment = SimpleNamespace(
        GAME_ROOMS=namespace,
        PUBLIC_GAME_URL="https://play.example/arcade/",
        ALLOWED_GAME_ORIGINS=f"{ALLOWED_ORIGIN}, https://other.example/",
    )
    auth = AuthState()

    async def require_user_id(request: Request) -> str:
        auth.calls.append(str(request.url))
        if auth.failure is not None:
            raise auth.failure
        return cast(str, auth.user_id)

    monkeypatch.setattr(rooms_module, "require_user_id", require_user_id)
    monkeypatch.setattr(rooms_module, "uuid4", lambda: UUID(ROOM_ID))
    monkeypatch.setattr(rooms_module, "_now_ms", lambda: NOW_MS)

    async with AsyncClient(
        transport=ASGITransport(app=_EnvironmentApp(app, environment)),
        base_url="https://api.example",
    ) as http:
        yield RoomAppHarness(http, environment, namespace, stub, auth)


async def test_room_client_retains_one_named_stub_and_decodes_every_success() -> None:
    stub = FakeRoomStub()
    namespace = FakeRoomNamespace(stub)

    client = RoomClient.from_request(_request_for_namespace(namespace), ROOM_ID)
    created = await client.create(
        ROOM_ID,
        "usr_creator",
        NOW_MS,
        GameDescriptor(game_type="2048", mode="1v1"),
    )
    fetched = await client.get_snapshot("usr_creator")
    joined = await client.join("usr_creator", NOW_MS)
    ticket = await client.issue_ticket("usr_creator", NOW_MS)

    assert namespace.get_by_name_calls == [ROOM_ID]
    assert created.model_dump(mode="json") == _snapshot()
    assert fetched.model_dump(mode="json") == _snapshot()
    assert joined.model_dump(mode="json") == _snapshot()
    assert ticket.model_dump(mode="json") == {
        "ticket": VALID_TICKET,
        "expires_at": EXPIRES_AT,
    }
    assert stub.create_calls == [
        {
            "room_id": ROOM_ID,
            "created_by": "usr_creator",
            "now_ms": NOW_MS,
            "game": CREATE_BODY,
        }
    ]
    assert stub.snapshot_calls == ["usr_creator"]
    assert stub.join_calls == [("usr_creator", NOW_MS)]
    assert stub.ticket_calls == [("usr_creator", NOW_MS)]


async def test_create_room_returns_invite_and_authoritative_snapshot(
    room_client: RoomAppHarness,
) -> None:
    response = await room_client.http.post(
        "/v1/rooms",
        headers={"Authorization": "Bearer opaque"},
        json=CREATE_BODY,
    )

    assert response.status_code == 201
    assert response.json() == {
        "room_id": ROOM_ID,
        "invite_url": f"https://play.example/arcade/join/{ROOM_ID}",
        "expires_at": EXPIRES_AT,
        "snapshot": _snapshot(),
    }
    assert room_client.auth.calls == ["https://api.example/v1/rooms"]
    assert room_client.namespace.get_by_name_calls == [ROOM_ID]
    assert room_client.stub.create_calls == [
        {
            "room_id": ROOM_ID,
            "created_by": "usr_creator",
            "now_ms": NOW_MS,
            "game": CREATE_BODY,
        }
    ]


async def test_create_room_accepts_better_auth_session_and_uses_canonical_identity(
    fake_d1: Any,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    token = "better-auth-session"
    session_url = "https://auth.runretroarcade.com/api/auth/get-session"
    canonical_user_id = "usr_0123456789abcdef0123456789abcdef"
    stub = FakeRoomStub()
    stub.create_result = _success(_snapshot(canonical_user_id))
    namespace = FakeRoomNamespace(stub)
    environment = SimpleNamespace(
        DB=fake_d1,
        GAME_ROOMS=namespace,
        AUTH_SESSION_URL=session_url,
        PUBLIC_GAME_URL="https://play.example/arcade/",
        ALLOWED_GAME_ORIGINS=ALLOWED_ORIGIN,
    )
    session_calls: list[tuple[str, str]] = []

    async def resolve_session(received_token: str, received_url: str) -> str:
        session_calls.append((received_token, received_url))
        assert received_token == token
        assert received_url == session_url
        return canonical_user_id

    monkeypatch.setattr("retro_backend.auth.resolve_auth_session", resolve_session)
    monkeypatch.setattr(rooms_module, "uuid4", lambda: UUID(ROOM_ID))
    monkeypatch.setattr(rooms_module, "_now_ms", lambda: NOW_MS)

    async with AsyncClient(
        transport=ASGITransport(app=_EnvironmentApp(app, environment)),
        base_url="https://api.example",
    ) as http:
        response = await http.post(
            "/v1/rooms",
            headers={"Authorization": f"Bearer {token}"},
            json=CREATE_BODY,
        )

    assert response.status_code == 201
    assert response.json() == {
        "room_id": ROOM_ID,
        "invite_url": f"https://play.example/arcade/join/{ROOM_ID}",
        "expires_at": EXPIRES_AT,
        "snapshot": _snapshot(canonical_user_id),
    }
    assert session_calls == [(token, session_url)]
    assert namespace.get_by_name_calls == [ROOM_ID]
    assert stub.create_calls == [
        {
            "room_id": ROOM_ID,
            "created_by": canonical_user_id,
            "now_ms": NOW_MS,
            "game": CREATE_BODY,
        }
    ]
    token_digest = hashlib.sha256(token.encode()).hexdigest()
    assert fake_d1.bind_calls == [
        (token_digest,),
        (token_digest,),
        (canonical_user_id, token_digest),
        (token_digest, canonical_user_id),
    ]
    assert len(fake_d1.first_calls) == 2
    assert "FROM auth_sessions WHERE token_hash = ?1" in fake_d1.first_calls[0]
    assert "FROM revoked_tokens WHERE token_hash = ?1" in fake_d1.first_calls[1]
    assert len(fake_d1.run_calls) == 2
    assert "INSERT INTO users" in fake_d1.run_calls[0]
    assert "INSERT INTO auth_sessions" in fake_d1.run_calls[1]


@pytest.mark.parametrize(
    ("method", "path", "expected_allow"),
    [
        ("GET", "/v1/rooms", "POST"),
        ("POST", f"/v1/rooms/{ROOM_ID}", "GET"),
        ("GET", f"/v1/rooms/{ROOM_ID}/join", "POST"),
        ("GET", f"/v1/rooms/{ROOM_ID}/tickets", "POST"),
    ],
)
async def test_room_routes_reject_invalid_methods_before_auth(
    room_client: RoomAppHarness,
    method: str,
    path: str,
    expected_allow: str,
) -> None:
    response = await room_client.http.request(method, path)

    assert response.status_code == 405
    assert response.headers["allow"] == expected_allow
    assert response.json() == _error("invalid_request", "Method not allowed")
    assert room_client.auth.calls == []
    assert room_client.namespace.get_by_name_calls == []


@pytest.mark.parametrize(
    ("method", "path", "origin", "expected_allow"),
    [
        ("BREW", "/v1/rooms", None, "POST"),
        ("PROPFIND", f"/v1/rooms/{ROOM_ID}", ALLOWED_ORIGIN, "GET"),
        ("FROBULATE", f"/v1/rooms/{ROOM_ID}/join", None, "POST"),
        ("BREW", f"/v1/rooms/{ROOM_ID}/tickets", ALLOWED_ORIGIN, "POST"),
    ],
)
async def test_arbitrary_room_methods_use_route_specific_policy(
    room_client: RoomAppHarness,
    method: str,
    path: str,
    origin: str | None,
    expected_allow: str,
) -> None:
    headers = {"Origin": origin} if origin is not None else None

    response = await room_client.http.request(method, path, headers=headers)

    assert response.status_code == 405
    assert response.headers["allow"] == expected_allow
    assert response.headers["vary"] == "Origin"
    assert response.json() == _error("invalid_request", "Method not allowed")
    if origin is None:
        assert "access-control-allow-origin" not in response.headers
    else:
        assert response.headers["access-control-allow-origin"] == origin
    assert room_client.auth.calls == []
    assert room_client.namespace.get_by_name_calls == []


async def test_arbitrary_room_method_rejects_origin_before_auth_or_allocation(
    room_client: RoomAppHarness,
) -> None:
    response = await room_client.http.request(
        "PROPFIND",
        f"/v1/rooms/{ROOM_ID}/join",
        headers={"Origin": "https://evil.example"},
    )

    assert response.status_code == 403
    assert response.headers["vary"] == "Origin"
    assert "allow" not in response.headers
    assert "access-control-allow-origin" not in response.headers
    assert response.json() == _error("unauthorized", "Unauthorized")
    assert room_client.auth.calls == []
    assert room_client.namespace.get_by_name_calls == []


@pytest.mark.parametrize(
    "path",
    [
        "/v1/rooms/not-a-uuid",
        f"/v1/rooms/{ROOM_ID}/neighbor",
    ],
)
async def test_rejected_origin_precedes_room_route_validation(
    room_client: RoomAppHarness,
    path: str,
) -> None:
    response = await room_client.http.get(
        path,
        headers={"Origin": "https://evil.example"},
    )

    assert response.status_code == 403
    assert response.json() == _error("unauthorized", "Unauthorized")
    assert response.headers["vary"] == "Origin"
    assert "access-control-allow-origin" not in response.headers
    assert room_client.auth.calls == []
    assert room_client.namespace.get_by_name_calls == []


async def test_non_room_extension_method_keeps_fastapi_405(
    room_client: RoomAppHarness,
) -> None:
    response = await room_client.http.request("BREW", "/health/live")

    assert response.status_code == 405
    assert response.headers["allow"] == "GET"
    assert "vary" not in response.headers
    assert response.json() == {"detail": "Method Not Allowed"}
    assert room_client.auth.calls == []
    assert room_client.namespace.get_by_name_calls == []


@pytest.mark.parametrize("method", ["GET", "BREW"])
@pytest.mark.parametrize("path", ["/v1/rooms-neighbor", "/v1/roomservice"])
async def test_non_room_siblings_are_not_shadowed_by_room_routes(
    room_client: RoomAppHarness,
    method: str,
    path: str,
) -> None:
    response = await room_client.http.request(method, path)

    assert response.status_code == 404
    assert "vary" not in response.headers
    assert response.json() == {"detail": "Not Found"}
    assert room_client.auth.calls == []
    assert room_client.namespace.get_by_name_calls == []


@pytest.mark.parametrize(
    ("method", "expected_status", "expected_message"),
    [
        ("GET", 404, "Room not found"),
        ("BREW", 405, "Method not allowed"),
    ],
)
async def test_slash_delimited_room_descendants_keep_room_envelopes(
    room_client: RoomAppHarness,
    method: str,
    expected_status: int,
    expected_message: str,
) -> None:
    response = await room_client.http.request(method, "/v1/rooms/not-a-uuid")

    assert response.status_code == expected_status
    assert response.headers["vary"] == "Origin"
    expected_code = "room_not_found" if method == "GET" else "invalid_request"
    assert response.json() == _error(expected_code, expected_message)
    assert room_client.auth.calls == []
    assert room_client.namespace.get_by_name_calls == []


@pytest.mark.parametrize(
    ("method", "path"),
    [
        ("GET", "/v1/rooms/not-a-uuid"),
        ("GET", "/v1/rooms/123E4567-E89B-42D3-A456-426614174000"),
        ("POST", "/v1/rooms/not-a-uuid/join"),
        ("POST", "/v1/rooms/not-a-uuid/tickets"),
    ],
)
async def test_noncanonical_room_ids_return_non_enumerating_404_before_auth(
    room_client: RoomAppHarness,
    method: str,
    path: str,
) -> None:
    response = await room_client.http.request(method, path)

    assert response.status_code == 404
    assert response.json() == _error("room_not_found", "Room not found")
    assert room_client.auth.calls == []
    assert room_client.namespace.get_by_name_calls == []


async def test_neighboring_room_path_is_a_stable_404(
    room_client: RoomAppHarness,
) -> None:
    for suffix in ("neighbor", "socket"):
        response = await room_client.http.get(f"/v1/rooms/{ROOM_ID}/{suffix}")

        assert response.status_code == 404
        assert response.json() == _error("invalid_request", "Room route not found")
    assert room_client.auth.calls == []
    assert room_client.namespace.get_by_name_calls == []


async def test_oversized_room_path_returns_414_before_auth(
    room_client: RoomAppHarness,
) -> None:
    response = await room_client.http.get(f"/v1/rooms/{'é' * 508}")

    assert response.status_code == 414
    assert response.json() == _error("invalid_request", "Request path is too long")
    assert room_client.auth.calls == []
    assert room_client.namespace.get_by_name_calls == []


async def test_room_path_accepts_exactly_1024_decoded_utf8_bytes(
    room_client: RoomAppHarness,
) -> None:
    response = await room_client.http.get(f"/v1/rooms/{'é' * 507}")

    assert response.status_code == 404
    assert response.json() == _error("room_not_found", "Room not found")
    assert room_client.auth.calls == []
    assert room_client.namespace.get_by_name_calls == []


@pytest.mark.parametrize(
    ("content", "content_type"),
    [
        (b"", "application/json"),
        (b"{not-json", "application/json"),
        (b'null', "application/json"),
        (b'{"game_type":"2048","mode":"1v1","mode":"1v1"}', "application/json"),
        (b'{"game_type":"2048","mode":NaN}', "application/json"),
        (b'{"game_type":"2048","mode":"1v1","extra":true}', "application/json"),
        (b'{"game_type":"2048","mode":"2v2"}', "application/json"),
        (b'{"game_type":"2048","mode":"1v1"}', "text/plain"),
        (b'{"game_type":"2048","mode":"1v1"}', ""),
        (b"\xff", "application/json"),
        (b"x" * 4_097, "application/json"),
    ],
)
async def test_authenticated_create_rejects_invalid_body_before_allocation(
    room_client: RoomAppHarness,
    content: bytes,
    content_type: str,
) -> None:
    response = await room_client.http.post(
        "/v1/rooms",
        content=content,
        headers={
            "Authorization": "Bearer opaque",
            "Content-Type": content_type,
        },
    )

    assert response.status_code == 400
    assert response.json() == _error("invalid_request", "Invalid room creation request")
    assert len(room_client.auth.calls) == 1
    assert room_client.namespace.get_by_name_calls == []


@pytest.mark.parametrize(
    "content_type_headers",
    [
        [
            ("Content-Type", "application/json"),
            ("Content-Type", "application/json"),
        ],
        [
            ("Content-Type", "application/json"),
            ("Content-Type", "text/plain"),
        ],
        [("Content-Type", "application/json, application/json")],
        [("Content-Type", "application/json, text/plain")],
        [("Content-Type", "application/json; charset=utf-8; charset=utf-8")],
    ],
)
async def test_create_rejects_ambiguous_content_type_after_authentication(
    room_client: RoomAppHarness,
    content_type_headers: list[tuple[str, str]],
) -> None:
    response = await room_client.http.post(
        "/v1/rooms",
        content=b'{"game_type":"2048","mode":"1v1"}',
        headers=[
            ("Authorization", "Bearer opaque"),
            *content_type_headers,
        ],
    )

    assert response.status_code == 400
    assert response.json() == _error("invalid_request", "Invalid room creation request")
    assert len(room_client.auth.calls) == 1
    assert room_client.namespace.get_by_name_calls == []


@pytest.mark.parametrize(
    "content_type",
    [
        "application/json; charset=utf-8",
        'Application/JSON; Charset="UTF-8"',
    ],
)
async def test_create_accepts_one_utf8_content_type_parameter(
    room_client: RoomAppHarness,
    content_type: str,
) -> None:
    response = await room_client.http.post(
        "/v1/rooms",
        content=b'{"game_type":"2048","mode":"1v1"}',
        headers={
            "Authorization": "Bearer opaque",
            "Content-Type": content_type,
        },
    )

    assert response.status_code == 201
    assert room_client.auth.calls == ["https://api.example/v1/rooms"]
    assert room_client.namespace.get_by_name_calls == [ROOM_ID]


async def test_authentication_precedes_room_allocation(
    room_client: RoomAppHarness,
) -> None:
    room_client.auth.failure = HTTPException(status_code=401, detail="private detail")

    response = await room_client.http.post(
        "/v1/rooms",
        json=CREATE_BODY,
        headers={"Authorization": "Bearer bad"},
    )

    assert response.status_code == 401
    assert response.json() == _error("unauthorized", "Authentication required")
    assert len(room_client.auth.calls) == 1
    assert room_client.namespace.get_by_name_calls == []


async def test_unauthenticated_create_rejects_before_parsing_malformed_body(
    room_client: RoomAppHarness,
) -> None:
    room_client.auth.failure = HTTPException(status_code=401, detail="private detail")

    response = await room_client.http.post(
        "/v1/rooms",
        content=b"{not-json",
        headers={
            "Authorization": "Bearer bad",
            "Content-Type": "application/json",
        },
    )

    assert response.status_code == 401
    assert response.headers["www-authenticate"] == "Bearer"
    assert response.json() == _error("unauthorized", "Authentication required")
    assert len(room_client.auth.calls) == 1
    assert room_client.namespace.get_by_name_calls == []


async def test_origin_rejects_before_auth_or_room_allocation(
    room_client: RoomAppHarness,
) -> None:
    response = await room_client.http.post(
        "/v1/rooms",
        headers={
            "Origin": "https://evil.example",
            "Authorization": "Bearer opaque",
        },
        json=CREATE_BODY,
    )

    assert response.status_code == 403
    assert response.json() == _error("unauthorized", "Unauthorized")
    assert response.headers["vary"] == "Origin"
    assert "access-control-allow-origin" not in response.headers
    assert room_client.auth.calls == []
    assert room_client.namespace.get_by_name_calls == []


@pytest.mark.parametrize(
    ("path", "expected_method"),
    [
        ("/v1/rooms", "POST"),
        (f"/v1/rooms/{ROOM_ID}", "GET"),
        (f"/v1/rooms/{ROOM_ID}/join", "POST"),
        (f"/v1/rooms/{ROOM_ID}/tickets", "POST"),
    ],
)
async def test_exact_room_preflights_echo_allowed_origin(
    room_client: RoomAppHarness,
    path: str,
    expected_method: str,
) -> None:
    response = await room_client.http.options(
        path,
        headers={"Origin": ALLOWED_ORIGIN},
    )

    assert response.status_code == 204
    assert response.content == b""
    assert response.headers["access-control-allow-origin"] == ALLOWED_ORIGIN
    assert response.headers["access-control-allow-methods"] == expected_method
    assert (
        response.headers["access-control-allow-headers"]
        == "Authorization, Content-Type"
    )
    assert response.headers["vary"] == "Origin"
    assert room_client.auth.calls == []
    assert room_client.namespace.get_by_name_calls == []


async def test_originless_room_preflight_is_rejected(
    room_client: RoomAppHarness,
) -> None:
    response = await room_client.http.options(f"/v1/rooms/{ROOM_ID}")

    assert response.status_code == 403
    assert response.json() == _error("unauthorized", "Unauthorized")
    assert response.headers["vary"] == "Origin"
    assert "access-control-allow-origin" not in response.headers
    assert room_client.auth.calls == []
    assert room_client.namespace.get_by_name_calls == []


@pytest.mark.parametrize(
    ("path", "expected_method"),
    [
        ("/v1/rooms/not-a-uuid", "GET"),
        ("/v1/rooms/not-a-uuid/join", "POST"),
        ("/v1/rooms/not-a-uuid/tickets", "POST"),
        ("/v1/rooms/not-a-uuid/socket", "GET"),
    ],
)
async def test_room_preflight_precedes_room_id_validation(
    room_client: RoomAppHarness,
    path: str,
    expected_method: str,
) -> None:
    response = await room_client.http.options(
        path,
        headers={"Origin": ALLOWED_ORIGIN},
    )

    assert response.status_code == 204
    assert response.headers["access-control-allow-origin"] == ALLOWED_ORIGIN
    assert response.headers["access-control-allow-methods"] == expected_method
    assert response.headers["vary"] == "Origin"
    assert room_client.auth.calls == []
    assert room_client.namespace.get_by_name_calls == []


async def test_unsupported_room_preflight_returns_structured_not_found(
    room_client: RoomAppHarness,
) -> None:
    path = f"/v1/rooms/{ROOM_ID}/neighbor"
    response = await room_client.http.options(
        path,
        headers={"Origin": ALLOWED_ORIGIN},
    )

    assert response.status_code == 404
    assert response.json() == _error("invalid_request", "Room route not found")
    assert "access-control-allow-methods" not in response.headers
    assert room_client.auth.calls == []
    assert room_client.namespace.get_by_name_calls == []


async def test_allowed_origin_is_echoed_on_room_response(
    room_client: RoomAppHarness,
) -> None:
    response = await room_client.http.get(
        f"/v1/rooms/{ROOM_ID}",
        headers={"Origin": ALLOWED_ORIGIN, "Authorization": "Bearer opaque"},
    )

    assert response.status_code == 200
    assert response.headers["access-control-allow-origin"] == ALLOWED_ORIGIN
    assert response.headers["vary"] == "Origin"


@pytest.mark.parametrize(
    ("origin", "configured"),
    [
        ("https://arcade.example:443", "https://arcade.example"),
        ("https://arcade.example", "HTTPS://ARCADE.EXAMPLE:443/"),
        ("https://xn--bcher-kva.example", "https://b\N{LATIN SMALL LETTER U WITH DIAERESIS}cher.example"),
        ("https://xn--fa-hia.de", "https://fa\N{LATIN SMALL LETTER SHARP S}.de"),
        ("https://xn--strae-oqa.de", "https://stra\N{LATIN SMALL LETTER SHARP S}e.de"),
        ("http://[::ffff:127.0.0.1]", "http://[::ffff:7f00:1]"),
    ],
)
async def test_origin_matches_browser_canonical_allowlist_entry(
    room_client: RoomAppHarness,
    origin: str,
    configured: str,
) -> None:
    room_client.environment.ALLOWED_GAME_ORIGINS = configured

    response = await room_client.http.get(
        f"/v1/rooms/{ROOM_ID}",
        headers={
            "Origin": origin,
            "Authorization": "Bearer opaque",
        },
    )

    assert response.status_code == 200
    assert response.headers["access-control-allow-origin"] == origin


async def test_invalid_alabel_is_not_an_allowed_origin(
    room_client: RoomAppHarness,
) -> None:
    room_client.environment.ALLOWED_GAME_ORIGINS = "https://xn--a.example"

    response = await room_client.http.get(
        f"/v1/rooms/{ROOM_ID}",
        headers={
            "Origin": "https://xn--a.example",
            "Authorization": "Bearer opaque",
        },
    )

    assert response.status_code == 403
    assert response.json() == _error("unauthorized", "Unauthorized")
    assert room_client.auth.calls == []
    assert room_client.namespace.get_by_name_calls == []


async def test_originless_native_room_request_is_allowed(
    room_client: RoomAppHarness,
) -> None:
    response = await room_client.http.get(
        f"/v1/rooms/{ROOM_ID}",
        headers={"Authorization": "Bearer opaque"},
    )

    assert response.status_code == 200
    assert response.json() == _snapshot()
    assert "access-control-allow-origin" not in response.headers


async def test_get_join_and_ticket_routes_call_the_retained_stub_once(
    room_client: RoomAppHarness,
) -> None:
    headers = {"Authorization": "Bearer opaque"}

    get_response = await room_client.http.get(f"/v1/rooms/{ROOM_ID}", headers=headers)
    join_response = await room_client.http.post(
        f"/v1/rooms/{ROOM_ID}/join", headers=headers
    )
    ticket_response = await room_client.http.post(
        f"/v1/rooms/{ROOM_ID}/tickets", headers=headers
    )

    assert get_response.status_code == 200
    assert get_response.json() == _snapshot()
    assert join_response.status_code == 200
    assert join_response.json() == _snapshot()
    assert ticket_response.status_code == 200
    assert ticket_response.json() == {
        "ticket": VALID_TICKET,
        "expires_at": EXPIRES_AT,
    }
    assert room_client.namespace.get_by_name_calls == [ROOM_ID, ROOM_ID, ROOM_ID]
    assert room_client.stub.snapshot_calls == ["usr_creator"]
    assert room_client.stub.join_calls == [("usr_creator", NOW_MS)]
    assert room_client.stub.ticket_calls == [("usr_creator", NOW_MS)]


@pytest.mark.parametrize("operation", ["snapshot", "ticket"])
async def test_participant_only_rpc_faults_are_preserved(
    room_client: RoomAppHarness,
    operation: str,
) -> None:
    public_message = "User has not joined this room"
    if operation == "snapshot":
        room_client.stub.snapshot_result = _failure("not_joined", public_message)
        response = await room_client.http.get(
            f"/v1/rooms/{ROOM_ID}",
            headers={"Authorization": "Bearer opaque"},
        )
    else:
        room_client.stub.ticket_result = _failure("not_joined", public_message)
        response = await room_client.http.post(
            f"/v1/rooms/{ROOM_ID}/tickets",
            headers={"Authorization": "Bearer opaque"},
        )

    assert response.status_code == 403
    assert response.json() == _error("not_joined", public_message)


async def test_rpc_failure_preserves_public_room_message(
    room_client: RoomAppHarness,
) -> None:
    room_client.stub.join_result = _failure("room_full", "Room is full")

    response = await room_client.http.post(
        f"/v1/rooms/{ROOM_ID}/join",
        headers={"Authorization": "Bearer opaque"},
    )

    assert response.status_code == 409
    assert response.json() == _error("room_full", "Room is full")


@pytest.mark.parametrize("failure_kind", ["binding", "rpc", "malformed"])
async def test_binding_rpc_and_malformed_envelope_fail_closed(
    room_client: RoomAppHarness,
    failure_kind: str,
) -> None:
    if failure_kind == "binding":
        room_client.namespace.failure = RuntimeError("binding secret")
    elif failure_kind == "rpc":
        room_client.stub.failure = RuntimeError("rpc secret")
    else:
        room_client.stub.snapshot_result = {
            "ok": True,
            "value": _snapshot(),
            "unexpected": "field",
        }

    response = await room_client.http.get(
        f"/v1/rooms/{ROOM_ID}",
        headers={"Authorization": "Bearer opaque"},
    )

    assert response.status_code == 503
    assert response.json() == _error("service_unavailable", "Room service unavailable")
    assert "secret" not in response.text


async def test_malformed_success_value_fails_closed(
    room_client: RoomAppHarness,
) -> None:
    room_client.stub.snapshot_result = _success({"room_id": ROOM_ID})

    response = await room_client.http.get(
        f"/v1/rooms/{ROOM_ID}",
        headers={"Authorization": "Bearer opaque"},
    )

    assert response.status_code == 503
    assert response.json() == _error("service_unavailable", "Room service unavailable")


@pytest.mark.parametrize(
    ("method", "path", "result_attribute", "malformed_value"),
    [
        ("POST", "/v1/rooms", "create_result", {"room_id": ROOM_ID}),
        ("GET", f"/v1/rooms/{ROOM_ID}", "snapshot_result", {"room_id": ROOM_ID}),
        ("POST", f"/v1/rooms/{ROOM_ID}/join", "join_result", {"room_id": ROOM_ID}),
        (
            "POST",
            f"/v1/rooms/{ROOM_ID}/tickets",
            "ticket_result",
            {"ticket": VALID_TICKET},
        ),
    ],
)
async def test_every_rpc_success_value_is_strictly_decoded(
    room_client: RoomAppHarness,
    method: str,
    path: str,
    result_attribute: str,
    malformed_value: object,
) -> None:
    setattr(room_client.stub, result_attribute, _success(malformed_value))
    if path == "/v1/rooms":
        response = await room_client.http.request(
            method,
            path,
            headers={"Authorization": "Bearer opaque"},
            json=CREATE_BODY,
        )
    else:
        response = await room_client.http.request(
            method,
            path,
            headers={"Authorization": "Bearer opaque"},
        )

    assert response.status_code == 503
    assert response.json() == _error("service_unavailable", "Room service unavailable")


@pytest.mark.parametrize(
    "failure",
    [
        {"ok": False, "error": {"code": "unknown", "message": "private"}},
        {
            "ok": False,
            "error": {
                "code": "room_full",
                "message": "private",
                "details": {"capacity": 2},
            },
        },
        {"ok": False, "error": {"code": "room_full", "message": ""}},
        {"ok": 1, "value": _snapshot()},
        ["not", "an", "envelope"],
    ],
)
async def test_unrecognized_or_malformed_rpc_failures_fail_closed(
    room_client: RoomAppHarness,
    failure: object,
) -> None:
    room_client.stub.join_result = failure

    response = await room_client.http.post(
        f"/v1/rooms/{ROOM_ID}/join",
        headers={"Authorization": "Bearer opaque"},
    )

    assert response.status_code == 503
    assert response.json() == _error("service_unavailable", "Room service unavailable")


@pytest.mark.parametrize(
    ("method", "path"),
    [
        ("POST", "/v1/rooms"),
        ("GET", f"/v1/rooms/{ROOM_ID}"),
        ("POST", f"/v1/rooms/{ROOM_ID}/join"),
        ("POST", f"/v1/rooms/{ROOM_ID}/tickets"),
    ],
)
async def test_every_authenticated_route_authenticates_before_allocation(
    room_client: RoomAppHarness,
    method: str,
    path: str,
) -> None:
    room_client.auth.failure = HTTPException(status_code=503, detail="private auth detail")
    if path == "/v1/rooms":
        response = await room_client.http.request(
            method,
            path,
            headers={"Authorization": "Bearer bad"},
            json=CREATE_BODY,
        )
    else:
        response = await room_client.http.request(
            method,
            path,
            headers={"Authorization": "Bearer bad"},
        )

    assert response.status_code == 503
    assert response.json() == _error(
        "service_unavailable",
        "Authentication service unavailable",
    )
    assert len(room_client.auth.calls) == 1
    assert room_client.namespace.get_by_name_calls == []


@pytest.mark.parametrize(
    "public_game_url",
    [
        None,
        "",
        "ftp://play.example/arcade",
        "https://user:secret@play.example/arcade",
        "https://play.example/arcade?next=bad",
        "https://play.example/arcade#fragment",
        " https://play.example/arcade",
        "https:///arcade",
    ],
)
async def test_invalid_public_game_url_fails_before_room_allocation(
    room_client: RoomAppHarness,
    public_game_url: str | None,
) -> None:
    if public_game_url is None:
        del room_client.environment.PUBLIC_GAME_URL
    else:
        room_client.environment.PUBLIC_GAME_URL = public_game_url

    response = await room_client.http.post(
        "/v1/rooms",
        headers={"Authorization": "Bearer opaque"},
        json=CREATE_BODY,
    )

    assert response.status_code == 503
    assert response.json() == _error("service_unavailable", "Room service unavailable")
    assert len(room_client.auth.calls) == 1
    assert room_client.namespace.get_by_name_calls == []


@pytest.mark.parametrize("now_ms", [-1, MAX_SAFE_INTEGER + 1, True])
async def test_create_rejects_unsafe_clock_values_before_allocation(
    room_client: RoomAppHarness,
    monkeypatch: pytest.MonkeyPatch,
    now_ms: object,
) -> None:
    monkeypatch.setattr(rooms_module, "_now_ms", lambda: now_ms)

    response = await room_client.http.post(
        "/v1/rooms",
        headers={"Authorization": "Bearer opaque"},
        json=CREATE_BODY,
    )

    assert response.status_code == 503
    assert response.json() == _error("service_unavailable", "Room service unavailable")
    assert room_client.namespace.get_by_name_calls == []


async def test_allowed_origin_is_preserved_on_public_rpc_failure(
    room_client: RoomAppHarness,
) -> None:
    room_client.stub.join_result = _failure("room_full", "Room is full")

    response = await room_client.http.post(
        f"/v1/rooms/{ROOM_ID}/join",
        headers={
            "Origin": ALLOWED_ORIGIN,
            "Authorization": "Bearer opaque",
        },
    )

    assert response.status_code == 409
    assert response.headers["access-control-allow-origin"] == ALLOWED_ORIGIN
    assert response.headers["vary"] == "Origin"
    assert response.json() == _error("room_full", "Room is full")


async def test_rejected_preflight_origin_precedes_auth_and_allocation(
    room_client: RoomAppHarness,
) -> None:
    response = await room_client.http.options(
        f"/v1/rooms/{ROOM_ID}",
        headers={"Origin": "https://evil.example"},
    )

    assert response.status_code == 403
    assert response.json() == _error("unauthorized", "Unauthorized")
    assert response.headers["vary"] == "Origin"
    assert "access-control-allow-origin" not in response.headers
    assert room_client.auth.calls == []
    assert room_client.namespace.get_by_name_calls == []


async def test_non_room_apis_keep_their_existing_response_envelopes(
    room_client: RoomAppHarness,
) -> None:
    health = await room_client.http.get("/health/live")
    missing = await room_client.http.get("/v1/not-a-room-route")

    assert health.status_code == 200
    assert health.json() == {"status": "healthy"}
    assert missing.status_code == 404
    assert missing.json() == {"detail": "Not Found"}
