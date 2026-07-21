import json
from collections.abc import Iterator, Mapping
from types import SimpleNamespace
from typing import override
from unittest.mock import ANY

import pytest

import retro_backend.worker_websocket as worker_websocket
from retro_backend.multiplayer.errors import PUBLIC_ERROR_MESSAGES, RoomErrorCode

ROOM_ID = "123e4567-e89b-42d3-a456-426614174000"
VALID_TICKET = "A" * 43
ALLOWED_ORIGIN = "https://game.example"
MAX_URL_COMPONENT_BYTES = 1024


class _HeadersConstructor:
    name = "Headers"


class WorkerHeaders:
    """Iterable FFI-shaped Headers proxy whose Python ``items`` must not be used."""

    constructor = _HeadersConstructor()

    def __init__(self, items: list[tuple[str, str]] | None = None) -> None:
        self._items = list(items or [])
        self.iterations = 0
        self.items_calls = 0

    def __iter__(self) -> Iterator[tuple[str, str]]:
        self.iterations += 1
        return iter(self._items)

    def items(self) -> Iterator[tuple[str, str]]:
        self.items_calls += 1
        raise AssertionError("Worker Headers.items() must not be called")


class ExplodingHeaders(WorkerHeaders):
    @override
    def __iter__(self) -> Iterator[tuple[str, str]]:
        raise AssertionError("noncandidate requests must not inspect headers")


class BrokenWorkerHeaders(WorkerHeaders):
    @override
    def __iter__(self) -> Iterator[tuple[str, str]]:
        self.iterations += 1
        raise RuntimeError("Worker Headers iteration failed")


class WorkerRawRequest:
    def __init__(
        self,
        url: object,
        *,
        method: object = "GET",
        headers: object | None = None,
    ) -> None:
        self.url = url
        self.method = method
        self.headers = WorkerHeaders() if headers is None else headers


class WorkerRequest:
    """Python Request wrapper whose convenience properties are intentionally unsafe."""

    def __init__(self, raw_request: WorkerRawRequest) -> None:
        self.js_object = raw_request
        self.wrapper_accesses: list[str] = []

    @property
    def url(self) -> object:
        self.wrapper_accesses.append("url")
        raise AssertionError("the raw js_object URL must be used")

    @property
    def method(self) -> object:
        self.wrapper_accesses.append("method")
        raise AssertionError("the raw js_object method must be used")

    @property
    def headers(self) -> object:
        self.wrapper_accesses.append("headers")
        raise AssertionError("the raw js_object headers must be used")


class FakeRoomStub:
    def __init__(self) -> None:
        self.response = object()
        self.fetch_calls: list[object] = []
        self.failure: Exception | None = None

    async def fetch(self, request: object) -> object:
        self.fetch_calls.append(request)
        if self.failure is not None:
            raise self.failure
        return self.response


class FakeUpgradeResponse:
    def __init__(self) -> None:
        self.body = "upgrade-body"
        self.status = 101
        self.status_text = "Switching Protocols"
        self.headers = {
            "x-upstream": "retained",
            "vary": "Accept-Encoding",
        }
        self.web_socket = object()


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


def _environment() -> tuple[SimpleNamespace, FakeRoomNamespace, FakeRoomStub]:
    stub = FakeRoomStub()
    namespace = FakeRoomNamespace(stub)
    environment = SimpleNamespace(
        GAME_ROOMS=namespace,
        ALLOWED_GAME_ORIGINS=f"{ALLOWED_ORIGIN}, https://other.example/",
    )
    return environment, namespace, stub


def _request(
    path: str,
    *,
    method: object = "GET",
    headers: list[tuple[str, str]] | None = None,
    wrapped: bool = False,
) -> WorkerRawRequest | WorkerRequest:
    raw_request = WorkerRawRequest(
        f"https://api.example{path}",
        method=method,
        headers=WorkerHeaders(headers),
    )
    return WorkerRequest(raw_request) if wrapped else raw_request


def _socket_path(room_id: str = ROOM_ID, query: str | None = None) -> str:
    path = f"/v1/rooms/{room_id}/socket"
    return path if query is None else f"{path}?{query}"


def _headers(response: object) -> dict[str, str]:
    raw_headers = getattr(response, "headers", None)
    assert isinstance(raw_headers, Mapping)
    return {str(key).casefold(): str(value) for key, value in raw_headers.items()}


def _assert_fault(
    response: object | None,
    *,
    status: int,
    code: RoomErrorCode,
    message: str | None = None,
    headers: dict[str, str] | None = None,
) -> None:
    assert response is not None
    assert getattr(response, "status", None) == status
    assert getattr(response, "body", None) == json.dumps(
        {"error": {"code": code, "message": message or PUBLIC_ERROR_MESSAGES[code]}},
        separators=(",", ":"),
    )
    expected_headers = {
        "content-type": "application/json",
        "vary": "Origin",
    }
    expected_headers.update(headers or {})
    assert _headers(response) == expected_headers


@pytest.mark.parametrize(
    "path",
    [
        "/health/live",
        "/v1/rooms",
        f"/v1/rooms/{ROOM_ID}",
        f"/v1/rooms/{ROOM_ID}/join",
        f"/v1/rooms/{ROOM_ID}/tickets",
        f"/v1/rooms/{ROOM_ID}/socket/extra",
        f"/v1/rooms/{ROOM_ID}/extra/socket",
        f"/wrong/v1/rooms/{ROOM_ID}/socket",
        f"/evil%2F/rooms/{ROOM_ID}/socket",
        f"/v1/evil%5C/{ROOM_ID}/socket",
        f"/v1/rooms/{ROOM_ID}/evil%252F",
    ],
)
async def test_noncandidate_paths_return_none_without_inspecting_headers_or_allocating(
    path: str,
) -> None:
    environment, namespace, stub = _environment()
    request = WorkerRawRequest(
        f"https://api.example{path}",
        method="DELETE",
        headers=ExplodingHeaders(),
    )

    response = await worker_websocket.handle_room_socket(request, environment)

    assert response is None
    assert namespace.get_by_name_calls == []
    assert stub.fetch_calls == []


async def test_non_string_or_unparseable_urls_preserve_the_asgi_flow() -> None:
    environment, namespace, _stub = _environment()

    non_string = await worker_websocket.handle_room_socket(
        WorkerRawRequest(123, headers=ExplodingHeaders()),
        environment,
    )
    malformed = await worker_websocket.handle_room_socket(
        WorkerRawRequest("https://[invalid/v1/rooms/id/socket", headers=ExplodingHeaders()),
        environment,
    )

    assert non_string is None
    assert malformed is None
    assert namespace.get_by_name_calls == []


@pytest.mark.parametrize(
    "encoded_delimiter",
    ["%2F", "%2f", "%5C", "%5c", "%252F", "%252f", "%255C", "%255c"],
)
@pytest.mark.parametrize(
    "path_template",
    [
        "/v1{delimiter}rooms/{room_id}/socket",
        "/v1/rooms{delimiter}{room_id}/socket",
        "/v1/rooms/{room_id}{delimiter}socket",
    ],
)
async def test_encoded_raw_boundary_aliases_are_room_not_found_without_allocation(
    encoded_delimiter: str,
    path_template: str,
) -> None:
    environment, namespace, stub = _environment()
    path = path_template.format(
        delimiter=encoded_delimiter,
        room_id=ROOM_ID,
    )

    response = await worker_websocket.handle_room_socket(
        _request(f"{path}?ticket={VALID_TICKET}", headers=[("Upgrade", "websocket")]),
        environment,
    )

    _assert_fault(response, status=404, code="room_not_found")
    assert namespace.get_by_name_calls == []
    assert stub.fetch_calls == []


async def test_rejected_origin_precedes_encoded_socket_alias() -> None:
    environment, namespace, stub = _environment()

    response = await worker_websocket.handle_room_socket(
        _request(
            f"/v1/rooms/{ROOM_ID}%2Fsocket?ticket={VALID_TICKET}",
            headers=[("Origin", "https://denied.example")],
        ),
        environment,
    )

    _assert_fault(response, status=403, code="unauthorized")
    assert namespace.get_by_name_calls == []
    assert stub.fetch_calls == []


@pytest.mark.parametrize(
    "encoded_delimiter",
    ["%2F", "%2f", "%5C", "%5c", "%252F", "%252f", "%255C", "%255c"],
)
@pytest.mark.parametrize(
    "path_template",
    [
        "/v1{delimiter}/rooms/{room_id}/socket",
        "/v1/rooms{delimiter}/{room_id}/socket",
        "/v1/rooms/{room_id}{delimiter}/socket",
        "/v1/rooms/{room_id}/socket{delimiter}",
    ],
)
async def test_encoded_delimiters_in_structural_segments_are_non_enumerating_404s(
    encoded_delimiter: str,
    path_template: str,
) -> None:
    environment, namespace, stub = _environment()
    path = path_template.format(
        delimiter=encoded_delimiter,
        room_id=ROOM_ID,
    )

    response = await worker_websocket.handle_room_socket(
        _request(f"{path}?ticket={VALID_TICKET}", headers=[("Upgrade", "websocket")]),
        environment,
    )

    _assert_fault(response, status=404, code="room_not_found")
    assert namespace.get_by_name_calls == []
    assert stub.fetch_calls == []


@pytest.mark.parametrize("extra_bytes", [0, 1])
async def test_candidate_path_enforces_the_decoded_utf8_1024_byte_boundary(
    extra_bytes: int,
) -> None:
    environment, namespace, _stub = _environment()
    fixed_bytes = len(b"/v1/rooms//socket")
    room_segment = "x" * (MAX_URL_COMPONENT_BYTES - fixed_bytes + extra_bytes)

    response = await worker_websocket.handle_room_socket(
        _request(_socket_path(room_segment)),
        environment,
    )

    if extra_bytes == 0:
        _assert_fault(response, status=404, code="room_not_found")
    else:
        _assert_fault(response, status=414, code="invalid_request")
    assert namespace.get_by_name_calls == []


@pytest.mark.parametrize("extra_bytes", [0, 1])
async def test_candidate_query_enforces_the_decoded_utf8_1024_byte_boundary(
    extra_bytes: int,
) -> None:
    environment, namespace, _stub = _environment()
    decoded_value_bytes = MAX_URL_COMPONENT_BYTES - len("ticket=") + extra_bytes
    encoded_value = "%41" * decoded_value_bytes

    response = await worker_websocket.handle_room_socket(
        _request(_socket_path(query=f"ticket={encoded_value}"), headers=[("Upgrade", "websocket")]),
        environment,
    )

    _assert_fault(response, status=401, code="invalid_ticket")
    assert namespace.get_by_name_calls == []


async def test_socket_query_limit_counts_the_raw_leading_question_mark(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    environment, namespace, stub = _environment()
    raw_query = "x" * MAX_URL_COMPONENT_BYTES
    parser_calls: list[str] = []

    def ticket_from_query(query: str, *, has_fragment: bool) -> str:
        assert not has_fragment
        parser_calls.append(query)
        return VALID_TICKET

    monkeypatch.setattr(worker_websocket, "_ticket_from_query", ticket_from_query)

    response = await worker_websocket.handle_room_socket(
        _request(
            _socket_path(query=raw_query),
            headers=[("Upgrade", "websocket")],
        ),
        environment,
    )

    _assert_fault(response, status=401, code="invalid_ticket")
    assert parser_calls == []
    assert namespace.get_by_name_calls == []
    assert stub.fetch_calls == []


@pytest.mark.parametrize(
    "room_id",
    [
        "",
        "not-a-uuid",
        ROOM_ID.upper(),
        f"{ROOM_ID}%2Fneighbor",
        f"{ROOM_ID}%FF",
    ],
)
async def test_malformed_or_noncanonical_candidate_room_ids_are_non_enumerating_404s(
    room_id: str,
) -> None:
    environment, namespace, _stub = _environment()

    response = await worker_websocket.handle_room_socket(
        _request(_socket_path(room_id, f"ticket={VALID_TICKET}")),
        environment,
    )

    _assert_fault(response, status=404, code="room_not_found")
    assert namespace.get_by_name_calls == []


async def test_originless_wrong_method_is_rejected_after_worker_header_parsing() -> None:
    environment, namespace, _stub = _environment()
    headers = WorkerHeaders()
    request = WorkerRawRequest(
        f"https://api.example{_socket_path(query='ticket=too-short')}",
        method="get",
        headers=headers,
    )

    response = await worker_websocket.handle_room_socket(request, environment)

    _assert_fault(
        response,
        status=405,
        code="invalid_request",
        message="Method not allowed",
        headers={"allow": "GET"},
    )
    assert headers.iterations == 1
    assert headers.items_calls == 0
    assert namespace.get_by_name_calls == []


async def test_allowed_origin_is_echoed_on_wrong_method_before_upgrade_or_ticket() -> None:
    environment, namespace, _stub = _environment()
    headers = WorkerHeaders([("Origin", ALLOWED_ORIGIN)])
    request = WorkerRawRequest(
        f"https://api.example{_socket_path(query='ticket=too-short')}",
        method="POST",
        headers=headers,
    )

    response = await worker_websocket.handle_room_socket(request, environment)

    _assert_fault(
        response,
        status=405,
        code="invalid_request",
        message="Method not allowed",
        headers={
            "access-control-allow-origin": ALLOWED_ORIGIN,
            "allow": "GET",
        },
    )
    assert headers.iterations == 1
    assert headers.items_calls == 0
    assert namespace.get_by_name_calls == []


@pytest.mark.parametrize("room_id", [ROOM_ID, "not-a-uuid"])
async def test_allowed_socket_preflight_precedes_room_id_validation(
    room_id: str,
) -> None:
    environment, namespace, stub = _environment()

    response = await worker_websocket.handle_room_socket(
        _request(
            _socket_path(room_id),
            method="OPTIONS",
            headers=[("Origin", ALLOWED_ORIGIN)],
        ),
        environment,
    )

    assert response is not None
    assert getattr(response, "status", None) == 204
    assert getattr(response, "body", "missing") is None
    assert _headers(response) == {
        "access-control-allow-origin": ALLOWED_ORIGIN,
        "access-control-allow-methods": "GET",
        "access-control-allow-headers": "Authorization, Content-Type",
        "vary": "Origin",
    }
    assert namespace.get_by_name_calls == []
    assert stub.fetch_calls == []


@pytest.mark.parametrize(
    "origin_headers",
    [
        [("Origin", f"{ALLOWED_ORIGIN}/path")],
        [("Origin", ALLOWED_ORIGIN), ("origin", ALLOWED_ORIGIN)],
    ],
    ids=["disallowed", "duplicate"],
)
async def test_rejected_origin_precedes_wrong_method_without_allow_or_acao(
    origin_headers: list[tuple[str, str]],
) -> None:
    environment, namespace, _stub = _environment()
    headers = WorkerHeaders(origin_headers)
    request = WorkerRawRequest(
        f"https://api.example{_socket_path(query='ticket=too-short')}",
        method="POST",
        headers=headers,
    )

    response = await worker_websocket.handle_room_socket(request, environment)

    _assert_fault(response, status=403, code="unauthorized")
    assert headers.iterations == 1
    assert headers.items_calls == 0
    assert namespace.get_by_name_calls == []


async def test_broken_worker_headers_fail_before_wrong_method() -> None:
    environment, namespace, _stub = _environment()
    headers = BrokenWorkerHeaders()
    request = WorkerRawRequest(
        f"https://api.example{_socket_path(query=f'ticket={VALID_TICKET}')}",
        method="POST",
        headers=headers,
    )

    response = await worker_websocket.handle_room_socket(request, environment)

    _assert_fault(response, status=503, code="service_unavailable")
    assert headers.iterations == 1
    assert headers.items_calls == 0
    assert namespace.get_by_name_calls == []


async def test_originless_native_request_is_forwarded_without_bearer_authentication() -> None:
    environment, namespace, stub = _environment()
    request = _request(
        _socket_path(query=f"ticket={VALID_TICKET}"),
        headers=[("Upgrade", "WebSocket")],
    )

    response = await worker_websocket.handle_room_socket(request, environment)

    assert response is stub.response
    assert namespace.get_by_name_calls == [ROOM_ID]
    assert stub.fetch_calls == [request]


async def test_allowed_origin_wraps_upgrade_response_without_losing_websocket_fields() -> None:
    environment, namespace, stub = _environment()
    upstream = FakeUpgradeResponse()
    stub.response = upstream
    request = _request(
        _socket_path(query=f"ticket={VALID_TICKET}"),
        headers=[
            ("Origin", ALLOWED_ORIGIN),
            ("Upgrade", "websocket"),
        ],
    )

    response = await worker_websocket.handle_room_socket(request, environment)

    assert response is not upstream
    assert getattr(response, "body", None) == upstream.body
    assert getattr(response, "status", None) == upstream.status
    assert getattr(response, "status_text", None) == upstream.status_text
    assert getattr(response, "web_socket", None) is upstream.web_socket
    assert _headers(response) == {
        "access-control-allow-origin": ALLOWED_ORIGIN,
        "vary": "Accept-Encoding, Origin",
        "x-upstream": "retained",
    }
    assert namespace.get_by_name_calls == [ROOM_ID]
    assert stub.fetch_calls == [request]


async def test_allowed_origin_uses_exact_allowlist_membership_and_echoes_on_errors() -> None:
    environment, namespace, _stub = _environment()

    response = await worker_websocket.handle_room_socket(
        _request(
            _socket_path(query=f"ticket={VALID_TICKET}"),
            headers=[("Origin", "https://other.example/")],
        ),
        environment,
    )

    _assert_fault(
        response,
        status=426,
        code="invalid_request",
        message="WebSocket upgrade required",
        headers={
            "access-control-allow-origin": "https://other.example/",
            "upgrade": "websocket",
        },
    )
    assert namespace.get_by_name_calls == []


@pytest.mark.parametrize(
    ("configured", "requested"),
    [
        ("HTTPS://GAME.EXAMPLE:443", "https://game.example/"),
        ("https://xn--fa-hia.de", "https://faß.de"),
        ("http://[::ffff:127.0.0.1]", "http://[::ffff:7f00:1]"),
    ],
)
async def test_allowed_origin_uses_browser_canonical_origin_matching(
    configured: str,
    requested: str,
) -> None:
    environment, namespace, _stub = _environment()
    environment.ALLOWED_GAME_ORIGINS = configured

    response = await worker_websocket.handle_room_socket(
        _request(
            _socket_path(query=f"ticket={VALID_TICKET}"),
            headers=[("Origin", requested)],
        ),
        environment,
    )

    _assert_fault(
        response,
        status=426,
        code="invalid_request",
        message="WebSocket upgrade required",
        headers={
            "access-control-allow-origin": requested,
            "upgrade": "websocket",
        },
    )
    assert namespace.get_by_name_calls == []


@pytest.mark.parametrize(
    "origin_headers",
    [
        [("Origin", f"{ALLOWED_ORIGIN}/path"), ("Upgrade", "websocket")],
        [("Origin", ALLOWED_ORIGIN), ("origin", ALLOWED_ORIGIN), ("Upgrade", "websocket")],
        [("Origin", f"{ALLOWED_ORIGIN}, {ALLOWED_ORIGIN}"), ("Upgrade", "websocket")],
    ],
)
async def test_disallowed_or_nonsingular_origins_fail_before_ticket_or_allocation(
    origin_headers: list[tuple[str, str]],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    environment, namespace, _stub = _environment()
    ticket_calls: list[object] = []

    def validate_ticket(raw_ticket: object) -> str:
        ticket_calls.append(raw_ticket)
        return VALID_TICKET

    monkeypatch.setattr(worker_websocket, "validate_ticket", validate_ticket)

    response = await worker_websocket.handle_room_socket(
        _request(_socket_path(query=f"ticket={VALID_TICKET}"), headers=origin_headers),
        environment,
    )

    _assert_fault(response, status=403, code="unauthorized")
    assert ticket_calls == []
    assert namespace.get_by_name_calls == []


async def test_missing_origin_allowlist_fails_closed_without_allocation() -> None:
    environment, namespace, _stub = _environment()
    del environment.ALLOWED_GAME_ORIGINS

    response = await worker_websocket.handle_room_socket(
        _request(
            _socket_path(query=f"ticket={VALID_TICKET}"),
            headers=[("Origin", ALLOWED_ORIGIN), ("Upgrade", "websocket")],
        ),
        environment,
    )

    _assert_fault(response, status=403, code="unauthorized")
    assert namespace.get_by_name_calls == []


@pytest.mark.parametrize(
    "upgrade_headers",
    [
        [],
        [("Upgrade", "not-websocket")],
        [("Upgrade", " websocket")],
        [("Upgrade", "websocket"), ("upgrade", "websocket")],
    ],
)
async def test_upgrade_header_must_be_one_exact_case_insensitive_websocket_value(
    upgrade_headers: list[tuple[str, str]],
) -> None:
    environment, namespace, _stub = _environment()

    response = await worker_websocket.handle_room_socket(
        _request(_socket_path(query=f"ticket={VALID_TICKET}"), headers=upgrade_headers),
        environment,
    )

    _assert_fault(
        response,
        status=426,
        code="invalid_request",
        message="WebSocket upgrade required",
        headers={"upgrade": "websocket"},
    )
    assert namespace.get_by_name_calls == []


@pytest.mark.parametrize(
    "query",
    [
        "",
        "ticket",
        "ticket=",
        f"ticket={VALID_TICKET}&ticket={VALID_TICKET}",
        f"ticket={VALID_TICKET}&extra=1",
        f"extra={VALID_TICKET}",
        f"ticket={VALID_TICKET}=",
        "ticket=%",
        "ticket=%GG",
        "ticket=%FF",
        "ticket=A+B",
    ],
)
async def test_query_must_be_one_strict_nonblank_ticket_pair(query: str) -> None:
    environment, namespace, _stub = _environment()

    response = await worker_websocket.handle_room_socket(
        _request(_socket_path(query=query), headers=[("Upgrade", "websocket")]),
        environment,
    )

    _assert_fault(response, status=401, code="invalid_ticket")
    assert namespace.get_by_name_calls == []


async def test_fragments_are_rejected_as_invalid_tickets() -> None:
    environment, namespace, _stub = _environment()

    response = await worker_websocket.handle_room_socket(
        _request(
            f"{_socket_path(query=f'ticket={VALID_TICKET}')}#fragment",
            headers=[("Upgrade", "websocket")],
        ),
        environment,
    )

    _assert_fault(response, status=401, code="invalid_ticket")
    assert namespace.get_by_name_calls == []


async def test_ticket_percent_decoding_is_strict_and_happens_before_validation(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    environment, namespace, stub = _environment()
    request = _request(
        _socket_path(query=f"ticket={'%41' * 43}"),
        headers=[("Upgrade", "websocket")],
    )
    validated: list[object] = []

    def validate_ticket(raw_ticket: object) -> str:
        validated.append(raw_ticket)
        return VALID_TICKET

    monkeypatch.setattr(worker_websocket, "validate_ticket", validate_ticket)

    response = await worker_websocket.handle_room_socket(request, environment)

    assert response is stub.response
    assert validated == [VALID_TICKET]
    assert namespace.get_by_name_calls == [ROOM_ID]
    assert stub.fetch_calls == [request]


async def test_js_object_is_unwrapped_and_exact_raw_request_and_response_are_preserved() -> None:
    environment, namespace, stub = _environment()
    raw_request = WorkerRawRequest(
        f"https://api.example{_socket_path(query=f'ticket={VALID_TICKET}')}",
        headers=WorkerHeaders(
            [
                ("Authorization", "Bearer ignored"),
                ("Upgrade", "wEbSoCkEt"),
            ]
        ),
    )
    request = WorkerRequest(raw_request)

    response = await worker_websocket.handle_room_socket(request, environment)

    assert response is stub.response
    assert request.wrapper_accesses == []
    assert namespace.get_by_name_calls == [ROOM_ID]
    assert stub.fetch_calls == [raw_request]
    assert isinstance(raw_request.headers, WorkerHeaders)
    assert raw_request.headers.items_calls == 0
    assert raw_request.headers.iterations == 1


async def test_ticket_format_validation_rejects_before_room_allocation() -> None:
    environment, namespace, _stub = _environment()

    response = await worker_websocket.handle_room_socket(
        _request(
            _socket_path(query="ticket=too-short"),
            headers=[("Upgrade", "websocket")],
        ),
        environment,
    )

    _assert_fault(response, status=401, code="invalid_ticket")
    assert namespace.get_by_name_calls == []


@pytest.mark.parametrize("failure_stage", ["binding", "fetch"])
async def test_binding_and_fetch_exceptions_become_redacted_service_unavailable(
    failure_stage: str,
) -> None:
    environment, namespace, stub = _environment()
    private_error = RuntimeError("private binding detail must not leak")
    if failure_stage == "binding":
        namespace.failure = private_error
    else:
        stub.failure = private_error

    response = await worker_websocket.handle_room_socket(
        _request(
            _socket_path(query=f"ticket={VALID_TICKET}"),
            headers=[("Upgrade", "websocket")],
        ),
        environment,
    )

    _assert_fault(response, status=503, code="service_unavailable")
    assert "private" not in str(getattr(response, "body", ""))
    assert namespace.get_by_name_calls == [ROOM_ID]
    assert stub.fetch_calls == ([] if failure_stage == "binding" else [ANY])
