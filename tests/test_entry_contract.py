import ast
import importlib
import re
import subprocess
import sys
import tomllib
from collections.abc import Iterator
from pathlib import Path
from types import ModuleType, SimpleNamespace

import pytest
from fastapi import HTTPException, Request

ROOT = Path(__file__).resolve().parents[1]
SOURCE_ROOT = ROOT / "src" / "retro_backend"
ENTRY_PATH = SOURCE_ROOT / "entry.py"
CHUNK_GATE_PATH = SOURCE_ROOT / "chunk_gate.py"
WRANGLER_PATH = ROOT / "wrangler.toml"
PYPROJECT_PATH = ROOT / "pyproject.toml"
UV_LOCK_PATH = ROOT / "uv.lock"
PYLOCK_PATH = ROOT / "pylock.toml"
GITIGNORE_PATH = ROOT / ".gitignore"
MAKEFILE_PATH = ROOT / "Makefile"
README_PATH = ROOT / "README.md"
NODE_VERSION_PATH = ROOT / ".node-version"
NODE_PREFLIGHT_PATH = ROOT / "scripts" / "node_preflight.py"
DEPLOYMENT_PREFLIGHT_PATH = ROOT / "scripts" / "deployment_preflight.py"
CI_PATH = ROOT / ".github" / "workflows" / "ci.yml"
DEPLOY_PATH = ROOT / ".github" / "workflows" / "deploy.yml"
R2_LIFECYCLE_PATH = ROOT / "cloudflare" / "r2-lifecycle.json"
ROOM_ID = "123e4567-e89b-42d3-a456-426614174000"
RESULTS_API_PATH = SOURCE_ROOT / "api" / "results.py"
AUTH_CACHE_PATH = SOURCE_ROOT / "auth_cache.py"
WORKER_HTTP_PATH = SOURCE_ROOT / "worker_http.py"
DESIGN_PATH = (
    ROOT
    / "docs"
    / "superpowers"
    / "specs"
    / "2026-07-17-cloudflare-python-backend-design.md"
)
PLAN_PATH = (
    ROOT
    / "docs"
    / "superpowers"
    / "plans"
    / "2026-07-17-cloudflare-python-backend.md"
)

LEGACY_PATHS = (
    ".dockerignore",
    "Dockerfile",
    "docker-compose.yml",
    "alembic.ini",
    "migrations/env.py",
    "migrations/script.py.mako",
    "migrations/versions",
    "src/retro_backend/api/routes.py",
    "src/retro_backend/config.py",
    "src/retro_backend/database.py",
    "src/retro_backend/main.py",
    "src/retro_backend/models.py",
    "src/retro_backend/redis_client.py",
    "src/retro_backend/worker.py",
    "tests/test_worker.py",
)


def _read(path: Path) -> str:
    return path.read_text(encoding="utf-8")


def _parse_python(path: Path) -> ast.Module:
    return ast.parse(_read(path), filename=str(path))


def _class(tree: ast.AST, name: str) -> ast.ClassDef:
    return next(
        node
        for node in ast.walk(tree)
        if isinstance(node, ast.ClassDef) and node.name == name
    )


def _async_method(class_node: ast.ClassDef, name: str) -> ast.AsyncFunctionDef:
    return next(
        node
        for node in class_node.body
        if isinstance(node, ast.AsyncFunctionDef) and node.name == name
    )


def _wrangler_config() -> dict[str, object]:
    return tomllib.loads(_read(WRANGLER_PATH))


def _workflow_run_commands(source: str) -> list[str]:
    return [
        match.group("command").strip().strip("\"'")
        for match in re.finditer(
            r"^\s*-\s+run:\s*(?P<command>[^#\n]+)",
            source,
            flags=re.MULTILINE,
        )
    ]


class EntryStreamReader:
    def __init__(
        self,
        chunks: list[bytes],
        *,
        events: list[str] | None = None,
        read_failure: Exception | None = None,
    ) -> None:
        self.chunks = list(chunks)
        self.events = events
        self.read_failure = read_failure
        self.read_calls = 0
        self.cancel_calls: list[str] = []
        self.release_lock_calls = 0

    async def read(self) -> SimpleNamespace:
        self.read_calls += 1
        if self.events is not None:
            self.events.append("body:read")
        if self.read_failure is not None:
            raise self.read_failure
        if not self.chunks:
            return SimpleNamespace(done=True, value=None)
        return SimpleNamespace(done=False, value=self.chunks.pop(0))

    async def cancel(self, reason: str) -> None:
        self.cancel_calls.append(reason)

    def releaseLock(self) -> None:
        self.release_lock_calls += 1
        if self.events is not None:
            self.events.append("body:releaseLock")


class EntryStream:
    def __init__(
        self,
        chunks: list[bytes],
        *,
        events: list[str] | None = None,
        get_reader_failure: Exception | None = None,
        read_failure: Exception | None = None,
    ) -> None:
        self.reader = EntryStreamReader(
            chunks,
            events=events,
            read_failure=read_failure,
        )
        self.events = events
        self.get_reader_failure = get_reader_failure
        self.get_reader_calls = 0
        self.cancel_calls: list[str] = []

    def getReader(self) -> EntryStreamReader:
        self.get_reader_calls += 1
        if self.events is not None:
            self.events.append("body:getReader")
        if self.get_reader_failure is not None:
            raise self.get_reader_failure
        return self.reader

    async def cancel(self, reason: str) -> None:
        self.cancel_calls.append(reason)


def _room_overflow_request(
    mode: str,
    origin_headers: list[tuple[str, str]],
    *,
    path: str = "/v1/rooms",
    method: str = "POST",
    body_limit: int = 4096,
    authorization: str | None = "Bearer opaque",
) -> tuple[SimpleNamespace, EntryStream]:
    headers = [("Content-Type", "application/json"), *origin_headers]
    if authorization is not None:
        headers.append(("Authorization", authorization))
    if mode == "declared":
        headers.append(("Content-Length", str(body_limit + 1)))
        chunks = [b"must-not-be-read"]
    else:
        chunks = [b"x" * body_limit, b"overflow", b"must-not-be-read"]
    stream = EntryStream(chunks)
    request = SimpleNamespace(
        url=f"https://retro.example{path}",
        method=method,
        headers=headers,
        body=stream,
        redirect="follow",
    )
    return request, stream


@pytest.fixture
def worker_entry_runtime(
    monkeypatch: pytest.MonkeyPatch,
) -> Iterator[tuple[ModuleType, list[tuple[object, object, object]], object]]:
    class RuntimeBlob:
        def __init__(self, body: bytes) -> None:
            self.js_object = body

    class RuntimeRequest:
        def __init__(self, request: object, **_options: object) -> None:
            self.js_object = request

    class RuntimeResponse:
        def __init__(
            self,
            body: object = None,
            *,
            status: int = 200,
            headers: dict[str, str] | None = None,
        ) -> None:
            self.body = body
            self.data = body
            self.status = status
            self.headers = {
                key.casefold(): value for key, value in (headers or {}).items()
            }
            self.js_object = self

        @staticmethod
        def from_json(
            data: object,
            *,
            status: int,
            headers: dict[str, str] | None = None,
        ) -> "RuntimeResponse":
            response_headers = {"content-type": "application/json"}
            response_headers.update(
                {key.casefold(): value for key, value in (headers or {}).items()}
            )
            return RuntimeResponse(
                data,
                status=status,
                headers=response_headers,
            )

    class RuntimeWorkerEntrypoint:
        def __init__(self, environment: object) -> None:
            self.env = environment

    asgi_calls: list[tuple[object, object, object]] = []
    asgi_response = object()

    async def asgi_fetch(
        app: object,
        request: object,
        environment: object,
    ) -> object:
        asgi_calls.append((app, request, environment))
        return asgi_response

    fake_asgi = ModuleType("asgi")
    fake_asgi.__dict__["fetch"] = asgi_fetch
    fake_workers = ModuleType("workers")
    fake_workers.__dict__["Blob"] = RuntimeBlob
    fake_workers.__dict__["Request"] = RuntimeRequest
    fake_workers.__dict__["Response"] = RuntimeResponse
    fake_workers.__dict__["WorkerEntrypoint"] = RuntimeWorkerEntrypoint
    monkeypatch.setitem(sys.modules, "asgi", fake_asgi)
    monkeypatch.setitem(sys.modules, "workers", fake_workers)

    previous_entry = sys.modules.pop("retro_backend.entry", None)
    try:
        entry = importlib.import_module("retro_backend.entry")

        async def require_entry_user_id(request: Request) -> str:
            if request.headers.get("Authorization") != "Bearer opaque":
                raise HTTPException(status_code=401, detail="private auth detail")
            return "usr_entry"

        monkeypatch.setattr(
            entry,
            "require_user_id",
            require_entry_user_id,
            raising=False,
        )
        yield entry, asgi_calls, asgi_response
    finally:
        sys.modules.pop("retro_backend.entry", None)
        if previous_entry is not None:
            sys.modules["retro_backend.entry"] = previous_entry


def test_entry_exposes_fastapi_fetch_queue_and_chunk_gate() -> None:
    tree = _parse_python(ENTRY_PATH)
    imports = {
        alias.name
        for node in tree.body
        if isinstance(node, ast.Import)
        for alias in node.names
    }
    imported_names = {
        (node.module, alias.name)
        for node in tree.body
        if isinstance(node, ast.ImportFrom)
        for alias in node.names
    }
    all_imported_names = {
        (node.module, alias.name)
        for node in ast.walk(tree)
        if isinstance(node, ast.ImportFrom)
        for alias in node.names
    }

    assert "asgi" in imports
    assert ("workers", "WorkerEntrypoint") in imported_names
    assert ("workers", "Blob") in imported_names
    assert ("workers", "Request") in imported_names
    assert ("workers", "Response") in imported_names
    assert ("retro_backend.app", "app") in imported_names
    assert (
        "retro_backend.consumers.dispatch",
        "dispatch_queue",
    ) in imported_names
    assert ("retro_backend.chunk_gate", "ChunkGate") in imported_names
    assert (
        "retro_backend.episode_assembler",
        "EpisodeAssembler",
    ) in imported_names
    assert ("retro_backend.multiplayer.room", "GameRoom") in imported_names
    assert (
        "retro_backend.worker_websocket",
        "handle_room_socket",
    ) in imported_names
    assert (
        "retro_backend.worker_http",
        "fetch_with_body_limit",
    ) in imported_names
    assert ("js", "caches") in all_imported_names
    assert (
        "retro_backend.auth_cache",
        "build_auth_cache_environment",
    ) in imported_names
    exports = next(
        node
        for node in tree.body
        if isinstance(node, ast.Assign)
        and any(isinstance(target, ast.Name) and target.id == "__all__" for target in node.targets)
    )
    assert ast.literal_eval(exports.value) == [
        "ChunkGate",
        "EpisodeAssembler",
        "GameRoom",
        "Default",
    ]

    default = _class(tree, "Default")
    assert [base.id for base in default.bases if isinstance(base, ast.Name)] == [
        "WorkerEntrypoint"
    ]
    assert [argument.arg for argument in _async_method(default, "fetch").args.args] == [
        "self",
        "request",
    ]
    queue_arguments = _async_method(default, "queue").args
    assert [argument.arg for argument in queue_arguments.args] == [
        "self",
        "batch",
    ]
    # The runtime calls queue(self, batch, env, ctx); without a catch-all every
    # batch raises TypeError and retries into the dead-letter queue.
    assert queue_arguments.vararg is not None

    source = _read(ENTRY_PATH)
    assert "return await fetch_with_body_limit(" in source
    assert "asgi_fetch=asgi.fetch" in source
    assert "request_factory=_rebuild_worker_request" in source
    assert "too_large_response=_too_large_response" in source
    assert "room_response_factory=_room_policy_response" in source
    assert "asgi.fetch(app, request" not in source
    assert "Blob(body).js_object" in source
    assert "Request(" in source
    assert ".js_object" in source
    assert "return bounded_request.js_object" in source
    assert 'getattr(request, "js_object", request)' in source
    assert "incoming = cast(_WorkerJsRequest, raw_request)" in source
    assert "incoming = cast(_WorkerJsRequest, request)" not in source
    assert "incoming.url" in source
    assert "method=cast(HTTPMethod, incoming.method)" in source
    assert "headers=headers" in source
    assert "redirect=incoming.redirect" in source
    assert "Response.from_json(" in source
    assert '{"detail": "request body exceeds maximum size"}' in source
    assert "status=413" in source
    room_policy = next(
        node
        for node in tree.body
        if isinstance(node, ast.AsyncFunctionDef)
        and node.name == "_room_policy_response"
    )
    message_assignment = next(
        node
        for node in ast.walk(room_policy)
        if isinstance(node, ast.Assign)
        and any(
            isinstance(target, ast.Name) and target.id == "message"
            for target in node.targets
        )
    )
    message_expression = message_assignment.value
    assert isinstance(message_expression, ast.IfExp)
    assert ast.literal_eval(message_expression.body) == "Invalid room creation request"
    assert ast.literal_eval(message_expression.orelse) == "Invalid request"
    assert isinstance(message_expression.test, ast.Compare)
    assert isinstance(message_expression.test.left, ast.Name)
    assert message_expression.test.left.id == "path"
    assert len(message_expression.test.ops) == 1
    assert isinstance(message_expression.test.ops[0], ast.Eq)
    assert [ast.literal_eval(node) for node in message_expression.test.comparators] == [
        "/v1/rooms"
    ]
    assert '{"error": {"code": "invalid_request", "message": message}}' in source
    assert "status=400" in source
    assert "build_auth_cache_environment(" in source
    assert "Cache-Control" in source
    assert "max-age=" in source
    assert "await dispatch_queue(batch, self.env)" in source


async def test_default_fetch_returns_valid_room_socket_before_body_limit_or_asgi(
    worker_entry_runtime: tuple[
        ModuleType,
        list[tuple[object, object, object]],
        object,
    ],
) -> None:
    entry, asgi_calls, _asgi_response = worker_entry_runtime
    socket_response = object()

    class RoomStub:
        def __init__(self) -> None:
            self.fetch_calls: list[object] = []

        async def fetch(self, request: object) -> object:
            self.fetch_calls.append(request)
            return socket_response

    class RoomNamespace:
        def __init__(self, stub: RoomStub) -> None:
            self.stub = stub
            self.get_by_name_calls: list[str] = []

        def getByName(self, room_id: str) -> RoomStub:
            self.get_by_name_calls.append(room_id)
            return self.stub

    class SocketRequest:
        url = (
            "https://retro.example/v1/rooms/123e4567-e89b-42d3-a456-426614174000/"
            f"socket?ticket={'A' * 43}"
        )
        method = "GET"
        headers = [("Upgrade", "websocket")]

        @property
        def body(self) -> object:
            raise AssertionError("socket request body must not be read")

    stub = RoomStub()
    namespace = RoomNamespace(stub)
    environment = SimpleNamespace(
        GAME_ROOMS=namespace,
        ALLOWED_GAME_ORIGINS="https://game.example",
    )
    request = SocketRequest()

    response = await entry.Default(environment).fetch(request)

    assert response is socket_response
    assert namespace.get_by_name_calls == ["123e4567-e89b-42d3-a456-426614174000"]
    assert stub.fetch_calls == [request]
    assert asgi_calls == []


async def test_default_fetch_sends_noncandidate_through_existing_asgi_path(
    worker_entry_runtime: tuple[
        ModuleType,
        list[tuple[object, object, object]],
        object,
    ],
) -> None:
    entry, asgi_calls, asgi_response = worker_entry_runtime
    environment = SimpleNamespace()
    request = SimpleNamespace(
        url="https://retro.example/health/live",
        method="GET",
        headers=[],
        body=None,
    )

    response = await entry.Default(environment).fetch(request)

    assert response is asgi_response
    assert asgi_calls == [(entry.app, request, environment)]


@pytest.mark.parametrize("overflow_mode", ["declared", "streamed"])
@pytest.mark.parametrize(
    ("method", "path", "body_limit"),
    [
        ("POST", "/v1/rooms", 4096),
        ("GET", f"/v1/rooms/{ROOM_ID}", 16_384),
        ("POST", f"/v1/rooms/{ROOM_ID}/join", 16_384),
        ("POST", f"/v1/rooms/{ROOM_ID}/tickets", 16_384),
    ],
)
@pytest.mark.parametrize(
    (
        "origin_headers",
        "environment",
        "expected_status",
        "expected_data",
        "expected_origin",
    ),
    [
        (
            [],
            SimpleNamespace(ALLOWED_GAME_ORIGINS="https://game.example"),
            400,
            {"error": {"code": "invalid_request", "message": "Invalid request"}},
            None,
        ),
        (
            [("Origin", "https://game.example")],
            SimpleNamespace(ALLOWED_GAME_ORIGINS="https://game.example"),
            400,
            {"error": {"code": "invalid_request", "message": "Invalid request"}},
            "https://game.example",
        ),
        (
            [("Origin", "https://evil.example")],
            SimpleNamespace(ALLOWED_GAME_ORIGINS="https://game.example"),
            403,
            {"error": {"code": "unauthorized", "message": "Unauthorized"}},
            None,
        ),
        (
            [("Origin", "https://game.example"), ("origin", "https://game.example")],
            SimpleNamespace(ALLOWED_GAME_ORIGINS="https://game.example"),
            403,
            {"error": {"code": "unauthorized", "message": "Unauthorized"}},
            None,
        ),
        (
            [("Origin", "https://game.example, https://other.example")],
            SimpleNamespace(
                ALLOWED_GAME_ORIGINS="https://game.example,https://other.example"
            ),
            403,
            {"error": {"code": "unauthorized", "message": "Unauthorized"}},
            None,
        ),
        (
            [("Origin", "https://game.example")],
            SimpleNamespace(),
            403,
            {"error": {"code": "unauthorized", "message": "Unauthorized"}},
            None,
        ),
        (
            [("Origin", "https://game.example")],
            SimpleNamespace(ALLOWED_GAME_ORIGINS=123),
            403,
            {"error": {"code": "unauthorized", "message": "Unauthorized"}},
            None,
        ),
    ],
)
async def test_room_overflow_applies_origin_policy_before_asgi_or_body_when_rejected(
    worker_entry_runtime: tuple[
        ModuleType,
        list[tuple[object, object, object]],
        object,
    ],
    overflow_mode: str,
    method: str,
    path: str,
    body_limit: int,
    origin_headers: list[tuple[str, str]],
    environment: object,
    expected_status: int,
    expected_data: dict[str, object],
    expected_origin: str | None,
) -> None:
    entry, asgi_calls, _asgi_response = worker_entry_runtime
    request, stream = _room_overflow_request(
        overflow_mode,
        origin_headers,
        path=path,
        method=method,
        body_limit=body_limit,
    )

    response = await entry.Default(environment).fetch(request)

    assert response.status == expected_status
    if expected_status == 400 and path == "/v1/rooms":
        expected_data = {
            "error": {
                "code": "invalid_request",
                "message": "Invalid room creation request",
            }
        }
    assert response.data == expected_data
    expected_headers = {
        "content-type": "application/json",
        "vary": "Origin",
    }
    if expected_origin is not None:
        expected_headers["access-control-allow-origin"] = expected_origin
    assert response.headers == expected_headers
    assert asgi_calls == []
    if expected_status == 400 and overflow_mode == "declared":
        assert stream.get_reader_calls == 0
        assert stream.cancel_calls == ["request body exceeds maximum size"]
    elif expected_status == 400:
        assert stream.get_reader_calls == 1
        assert stream.reader.read_calls == 2
        assert stream.reader.cancel_calls == ["request body exceeds maximum size"]
        assert stream.reader.release_lock_calls == 1
    else:
        assert stream.get_reader_calls == 0
        assert stream.cancel_calls == []
        assert stream.reader.read_calls == 0


@pytest.mark.parametrize("overflow_mode", ["declared", "streamed"])
async def test_unauthenticated_room_create_overflow_rejects_before_body_access(
    worker_entry_runtime: tuple[
        ModuleType,
        list[tuple[object, object, object]],
        object,
    ],
    overflow_mode: str,
) -> None:
    entry, asgi_calls, _asgi_response = worker_entry_runtime
    request, stream = _room_overflow_request(
        overflow_mode,
        [],
        authorization=None,
    )
    environment = SimpleNamespace(ALLOWED_GAME_ORIGINS="https://game.example")

    response = await entry.Default(environment).fetch(request)

    assert response.status == 401
    assert response.data == {
        "error": {"code": "unauthorized", "message": "Authentication required"}
    }
    assert response.headers == {
        "content-type": "application/json",
        "vary": "Origin",
        "www-authenticate": "Bearer",
    }
    assert asgi_calls == []
    assert stream.get_reader_calls == 0
    assert stream.cancel_calls == []
    assert stream.reader.read_calls == 0


@pytest.mark.parametrize(
    "failure_mode",
    [
        "malformed-length",
        "leading-zero-length",
        "unsafe-length",
        "get-reader",
        "read",
    ],
)
async def test_authenticated_room_create_transport_failures_are_invalid_requests(
    worker_entry_runtime: tuple[
        ModuleType,
        list[tuple[object, object, object]],
        object,
    ],
    monkeypatch: pytest.MonkeyPatch,
    failure_mode: str,
) -> None:
    entry, asgi_calls, _asgi_response = worker_entry_runtime
    events: list[str] = []
    content_length = {
        "malformed-length": "secret",
        "leading-zero-length": "01",
        "unsafe-length": "9007199254740992",
    }.get(failure_mode)
    stream = EntryStream(
        [b'{"game_type":"2048","mode":"1v1"}'],
        events=events,
        get_reader_failure=(
            RuntimeError("request stream is locked")
            if failure_mode == "get-reader"
            else None
        ),
        read_failure=(
            RuntimeError("request stream read failed")
            if failure_mode == "read"
            else None
        ),
    )
    headers = [
        ("Content-Type", "application/json"),
        ("Authorization", "Bearer opaque"),
    ]
    if content_length is not None:
        headers.append(("Content-Length", content_length))
    request = SimpleNamespace(
        url="https://retro.example/v1/rooms",
        method="POST",
        headers=headers,
        body=stream,
        redirect="follow",
    )
    environment = SimpleNamespace(ALLOWED_GAME_ORIGINS="https://game.example")

    async def require_ordered_user_id(_request: Request) -> str:
        events.append("auth")
        return "usr_entry"

    monkeypatch.setattr(entry, "require_user_id", require_ordered_user_id)

    try:
        response = await entry.Default(environment).fetch(request)
    except Exception as error:
        pytest.fail(f"room create transport failure escaped: {error!r}", pytrace=False)

    assert response.status == 400
    assert response.data == {
        "error": {
            "code": "invalid_request",
            "message": "Invalid room creation request",
        }
    }
    assert response.headers == {
        "content-type": "application/json",
        "vary": "Origin",
    }
    assert asgi_calls == []
    assert events[0] == "auth"
    if failure_mode.endswith("length"):
        assert stream.get_reader_calls == 0
    elif failure_mode == "get-reader":
        assert events == ["auth", "body:getReader"]
    else:
        assert events == [
            "auth",
            "body:getReader",
            "body:read",
            "body:releaseLock",
        ]


async def test_authenticated_room_create_rejects_malformed_length_without_body(
    worker_entry_runtime: tuple[
        ModuleType,
        list[tuple[object, object, object]],
        object,
    ],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    entry, asgi_calls, _asgi_response = worker_entry_runtime
    events: list[str] = []
    request = SimpleNamespace(
        url="https://retro.example/v1/rooms",
        method="POST",
        headers=[
            ("Authorization", "Bearer opaque"),
            ("Content-Length", "secret"),
        ],
        body=None,
        redirect="follow",
    )
    environment = SimpleNamespace(ALLOWED_GAME_ORIGINS="https://game.example")

    async def require_ordered_user_id(_request: Request) -> str:
        events.append("auth")
        return "usr_entry"

    monkeypatch.setattr(entry, "require_user_id", require_ordered_user_id)

    response = await entry.Default(environment).fetch(request)

    assert response.status == 400
    assert response.data == {
        "error": {
            "code": "invalid_request",
            "message": "Invalid room creation request",
        }
    }
    assert response.headers == {
        "content-type": "application/json",
        "vary": "Origin",
    }
    assert events == ["auth"]
    assert asgi_calls == []


@pytest.mark.parametrize(
    ("method", "origin_headers", "expected_status", "expected_data", "expected_headers"),
    [
        (
            "OPTIONS",
            [("Origin", "https://game.example")],
            204,
            None,
            {
                "access-control-allow-origin": "https://game.example",
                "access-control-allow-methods": "GET",
                "access-control-allow-headers": "Authorization, Content-Type",
                "vary": "Origin",
            },
        ),
        (
            "POST",
            [],
            405,
            {"error": {"code": "invalid_request", "message": "Method not allowed"}},
            {
                "allow": "GET",
                "content-type": "application/json",
                "vary": "Origin",
            },
        ),
    ],
)
async def test_malformed_room_id_policy_precedes_declared_body_overflow(
    worker_entry_runtime: tuple[
        ModuleType,
        list[tuple[object, object, object]],
        object,
    ],
    method: str,
    origin_headers: list[tuple[str, str]],
    expected_status: int,
    expected_data: dict[str, object] | None,
    expected_headers: dict[str, str],
) -> None:
    entry, asgi_calls, _asgi_response = worker_entry_runtime
    request, stream = _room_overflow_request(
        "declared",
        origin_headers,
        path="/v1/rooms/not-a-room-id",
        method=method,
        body_limit=16_384,
    )
    environment = SimpleNamespace(ALLOWED_GAME_ORIGINS="https://game.example")

    response = await entry.Default(environment).fetch(request)

    assert response.status == expected_status
    assert response.data == expected_data
    assert response.headers == expected_headers
    assert asgi_calls == []
    assert stream.get_reader_calls == 0
    assert stream.cancel_calls == []


@pytest.mark.parametrize("overflow_mode", ["declared", "streamed"])
@pytest.mark.parametrize(
    ("path", "allowed_method", "wrong_method"),
    [
        ("/v1/rooms", "POST", "GET"),
        (f"/v1/rooms/{ROOM_ID}", "GET", "POST"),
        (f"/v1/rooms/{ROOM_ID}/join", "POST", "GET"),
        (f"/v1/rooms/{ROOM_ID}/tickets", "POST", "GET"),
    ],
)
@pytest.mark.parametrize("request_kind", ["options", "wrong", "extension"])
@pytest.mark.parametrize(
    ("origin_headers", "environment", "expected_origin", "origin_rejected"),
    [
        (
            [],
            SimpleNamespace(ALLOWED_GAME_ORIGINS="https://game.example"),
            None,
            False,
        ),
        (
            [("Origin", "https://game.example")],
            SimpleNamespace(ALLOWED_GAME_ORIGINS="https://game.example"),
            "https://game.example",
            False,
        ),
        (
            [("Origin", "https://evil.example")],
            SimpleNamespace(ALLOWED_GAME_ORIGINS="https://game.example"),
            None,
            True,
        ),
    ],
)
async def test_room_method_policy_precedes_declared_and_streaming_bodies(
    worker_entry_runtime: tuple[
        ModuleType,
        list[tuple[object, object, object]],
        object,
    ],
    overflow_mode: str,
    path: str,
    allowed_method: str,
    wrong_method: str,
    request_kind: str,
    origin_headers: list[tuple[str, str]],
    environment: object,
    expected_origin: str | None,
    origin_rejected: bool,
) -> None:
    entry, asgi_calls, _asgi_response = worker_entry_runtime
    method = {
        "options": "OPTIONS",
        "wrong": wrong_method,
        "extension": "PROPFIND",
    }[request_kind]
    request, stream = _room_overflow_request(
        overflow_mode,
        origin_headers,
        path=path,
        method=method,
        body_limit=16_384,
    )

    response = await entry.Default(environment).fetch(request)

    if origin_rejected or (request_kind == "options" and expected_origin is None):
        assert response.status == 403
        assert response.data == {
            "error": {"code": "unauthorized", "message": "Unauthorized"}
        }
        expected_headers = {
            "content-type": "application/json",
            "vary": "Origin",
        }
    elif request_kind == "options":
        assert response.status == 204
        assert response.body is None
        expected_headers = {
            "access-control-allow-methods": allowed_method,
            "access-control-allow-headers": "Authorization, Content-Type",
            "vary": "Origin",
        }
        if expected_origin is not None:
            expected_headers["access-control-allow-origin"] = expected_origin
    else:
        assert response.status == 405
        assert response.data == {
            "error": {"code": "invalid_request", "message": "Method not allowed"}
        }
        expected_headers = {
            "allow": allowed_method,
            "content-type": "application/json",
            "vary": "Origin",
        }
        if expected_origin is not None:
            expected_headers["access-control-allow-origin"] = expected_origin
    assert response.headers == expected_headers
    assert asgi_calls == []
    assert stream.get_reader_calls == 0
    assert stream.cancel_calls == []
    assert stream.reader.read_calls == 0


async def test_non_room_neighbor_reaches_asgi_without_room_origin_policy(
    worker_entry_runtime: tuple[
        ModuleType,
        list[tuple[object, object, object]],
        object,
    ],
) -> None:
    entry, asgi_calls, asgi_response = worker_entry_runtime
    environment = SimpleNamespace(ALLOWED_GAME_ORIGINS="https://game.example")
    request = SimpleNamespace(
        url="https://retro.example/v1/rooms-neighbor",
        method="POST",
        headers=[("Origin", "https://evil.example")],
        body=None,
        redirect="follow",
    )

    response = await entry.Default(environment).fetch(request)

    assert response is asgi_response
    assert asgi_calls == [(entry.app, request, environment)]


@pytest.mark.parametrize(
    "path",
    [
        "/v1/rooms/",
        f"/v1/rooms/{ROOM_ID}/join/",
        f"/v1/rooms/{ROOM_ID}/ticket",
    ],
)
async def test_unsupported_room_paths_receive_origin_policy_before_body(
    worker_entry_runtime: tuple[
        ModuleType,
        list[tuple[object, object, object]],
        object,
    ],
    path: str,
) -> None:
    entry, asgi_calls, _asgi_response = worker_entry_runtime
    environment = SimpleNamespace(ALLOWED_GAME_ORIGINS="https://game.example")
    request = SimpleNamespace(
        url=f"https://retro.example{path}",
        method="POST",
        headers=[("Origin", "https://evil.example")],
        body=EntryStream([b"must-not-be-read"]),
        redirect="follow",
    )

    response = await entry.Default(environment).fetch(request)

    assert response.status == 403
    assert response.data == {
        "error": {"code": "unauthorized", "message": "Unauthorized"}
    }
    assert response.headers == {
        "content-type": "application/json",
        "vary": "Origin",
    }
    assert asgi_calls == []
    assert request.body.get_reader_calls == 0


async def test_unsupported_room_options_requires_an_allowed_origin(
    worker_entry_runtime: tuple[
        ModuleType,
        list[tuple[object, object, object]],
        object,
    ],
) -> None:
    entry, asgi_calls, _asgi_response = worker_entry_runtime
    request, stream = _room_overflow_request(
        "declared",
        [],
        path="/v1/rooms/unsupported/route",
        method="OPTIONS",
        body_limit=16_384,
    )
    environment = SimpleNamespace(ALLOWED_GAME_ORIGINS="https://game.example")

    response = await entry.Default(environment).fetch(request)

    assert response.status == 403
    assert response.data == {
        "error": {"code": "unauthorized", "message": "Unauthorized"}
    }
    assert response.headers == {
        "content-type": "application/json",
        "vary": "Origin",
    }
    assert asgi_calls == []
    assert stream.get_reader_calls == 0
    assert stream.cancel_calls == []


@pytest.mark.parametrize(
    "path",
    [
        "/v1/rooms/not-a-room-id",
        f"/v1/rooms/{ROOM_ID.upper()}",
    ],
)
async def test_malformed_room_ids_still_receive_outer_origin_policy(
    worker_entry_runtime: tuple[
        ModuleType,
        list[tuple[object, object, object]],
        object,
    ],
    path: str,
) -> None:
    entry, asgi_calls, _asgi_response = worker_entry_runtime
    environment = SimpleNamespace(ALLOWED_GAME_ORIGINS="https://game.example")
    request = SimpleNamespace(
        url=f"https://retro.example{path}",
        method="GET",
        headers=[("Origin", "https://evil.example")],
        body=None,
        redirect="follow",
    )

    response = await entry.Default(environment).fetch(request)

    assert response.status == 403
    assert response.data == {
        "error": {"code": "unauthorized", "message": "Unauthorized"}
    }
    assert response.headers == {
        "content-type": "application/json",
        "vary": "Origin",
    }
    assert asgi_calls == []


@pytest.mark.parametrize("overflow_mode", ["declared", "streamed"])
async def test_room_overflow_hides_allowlist_access_failures(
    worker_entry_runtime: tuple[
        ModuleType,
        list[tuple[object, object, object]],
        object,
    ],
    overflow_mode: str,
) -> None:
    entry, asgi_calls, _asgi_response = worker_entry_runtime

    class ExplodingEnvironment:
        @property
        def ALLOWED_GAME_ORIGINS(self) -> object:
            raise RuntimeError("private binding failure")

    request, stream = _room_overflow_request(
        overflow_mode,
        [("Origin", "https://game.example")],
    )

    response = await entry.Default(ExplodingEnvironment()).fetch(request)

    assert response.status == 503
    assert response.data == {
        "error": {
            "code": "service_unavailable",
            "message": "Room service unavailable",
        }
    }
    assert response.headers == {
        "content-type": "application/json",
        "vary": "Origin",
    }
    assert asgi_calls == []
    assert stream.get_reader_calls == 0
    assert stream.cancel_calls == []


async def test_encoded_room_alias_returns_canonical_room_response_without_body_read(
    worker_entry_runtime: tuple[
        ModuleType,
        list[tuple[object, object, object]],
        object,
    ],
) -> None:
    entry, asgi_calls, _asgi_response = worker_entry_runtime
    request, stream = _room_overflow_request(
        "streamed",
        [("Origin", "https://game.example")],
        path="/v1%252Frooms",
    )
    environment = SimpleNamespace(ALLOWED_GAME_ORIGINS="https://game.example")

    response = await entry.Default(environment).fetch(request)

    assert response.status == 404
    assert response.data == {
        "error": {"code": "invalid_request", "message": "Room route not found"}
    }
    assert response.headers == {
        "content-type": "application/json",
        "vary": "Origin",
        "access-control-allow-origin": "https://game.example",
    }
    assert asgi_calls == []
    assert stream.get_reader_calls == 0


@pytest.mark.parametrize(
    ("path", "content_length", "environment"),
    [
        (
            "/v1/game-results",
            "16385",
            SimpleNamespace(MAX_RESULT_BYTES="16384"),
        ),
        (
            "/v1/game-event-chunks",
            "262145",
            SimpleNamespace(MAX_CHUNK_BYTES="262144"),
        ),
        (
            "/v1/future-body-route",
            "16385",
            SimpleNamespace(MAX_RESULT_BYTES="16384"),
        ),
        (
            "/v1/rooms-neighbor",
            "16385",
            SimpleNamespace(MAX_RESULT_BYTES="16384"),
        ),
    ],
)
async def test_non_room_overflow_keeps_legacy_413_without_room_cors(
    worker_entry_runtime: tuple[
        ModuleType,
        list[tuple[object, object, object]],
        object,
    ],
    path: str,
    content_length: str,
    environment: object,
) -> None:
    entry, asgi_calls, _asgi_response = worker_entry_runtime
    stream = EntryStream([b"must-not-be-read"])
    request = SimpleNamespace(
        url=f"https://retro.example{path}",
        method="POST",
        headers=[
            ("Content-Length", content_length),
            ("Origin", "https://evil.example"),
        ],
        body=stream,
        redirect="follow",
    )

    response = await entry.Default(environment).fetch(request)

    assert response.status == 413
    assert response.data == {"detail": "request body exceeds maximum size"}
    assert response.headers == {"content-type": "application/json"}
    assert asgi_calls == []
    assert stream.get_reader_calls == 0
    assert stream.cancel_calls == ["request body exceeds maximum size"]


def test_worker_http_boundary_is_packaged_as_python_runtime_source() -> None:
    source = _read(WORKER_HTTP_PATH)

    assert "async def fetch_with_body_limit(" in source
    assert "content-length" in source
    assert "getReader" in source
    assert "await _cancel(reader)" in source
    assert "byteLength" in source
    assert 'asgi_request = getattr(request, "js_object", request)' in source
    assert "await asgi_fetch(app, asgi_request, environment)" in source
    assert "request_factory(request, raw_body, headers)" in source
    assert "await asgi_fetch(app, bounded_request, environment)" in source


def test_docs_place_body_limit_before_asgi_buffering_and_fastapi_auth() -> None:
    readme = _read(README_PATH)
    design = _read(DESIGN_PATH)
    plan = _read(PLAN_PATH)

    for source in (readme, design, plan):
        normalized = source.casefold()
        assert "worker fetch boundary" in normalized
        assert "asgi" in normalized
        assert "fastapi" in normalized
    combined = "\n".join((readme, design, plan)).casefold()
    assert "authenticates before reading" not in combined
    assert "both routes authenticate before reading" not in combined
    assert "return await asgi.fetch(app, request, self.env)" not in combined
    assert "fetch_with_body_limit" in plan


def test_chunk_gate_uses_explicit_runtime_base_branch() -> None:
    tree = _parse_python(CHUNK_GATE_PATH)
    platform_branch = next(
        node
        for node in ast.walk(tree)
        if isinstance(node, ast.If)
        and isinstance(node.test, ast.Compare)
        and ast.unparse(node.test) == "sys.platform == 'emscripten'"
    )
    runtime_imports = {
        (node.module, alias.name)
        for node in platform_branch.body
        if isinstance(node, ast.ImportFrom)
        for alias in node.names
    }

    assert runtime_imports == {("workers", "DurableObject")}
    assert _class(ast.Module(body=platform_branch.orelse, type_ignores=[]), "DurableObject")
    assert not any(
        isinstance(node, ast.Try)
        and any(
            isinstance(child, ast.ImportFrom) and child.module == "workers"
            for child in ast.walk(node)
        )
        for node in ast.walk(tree)
    )


def test_chunk_gate_exports_runtime_durable_object_rpc() -> None:
    tree = _parse_python(CHUNK_GATE_PATH)
    chunk_gate = _class(tree, "ChunkGate")

    assert [base.id for base in chunk_gate.bases if isinstance(base, ast.Name)] == [
        "DurableObject"
    ]

    expected_signatures = {
        "reserve": [
            "self",
            "payload_sha256",
            "lease_token",
            "now_ms",
            "lease_ms",
        ],
        "accept": [
            "self",
            "payload_sha256",
            "lease_token",
            "acknowledgment",
            "now_ms",
            "dedupe_ttl_ms",
        ],
        "release": ["self", "payload_sha256", "lease_token"],
        "alarm": ["self"],
    }
    for method_name, arguments in expected_signatures.items():
        method = _async_method(chunk_gate, method_name)
        assert [argument.arg for argument in method.args.args] == arguments


def test_wrangler_declares_worker_identity_and_observability() -> None:
    config = _wrangler_config()

    assert config["name"] == "retro-backend"
    assert config["main"] == "src/retro_backend/entry.py"
    assert config["base_dir"] == "src"
    assert config["compatibility_date"] == "2026-07-17"
    assert config["compatibility_flags"] == ["python_workers"]
    assert config["observability"] == {
        "enabled": True,
        "head_sampling_rate": 1.0,
    }
    assert config["workers_dev"] is False
    assert config["routes"] == [
        {"pattern": "api.runretroarcade.com", "custom_domain": True}
    ]


def test_wrangler_binds_d1_r2_and_gameplay_dlq_archiver() -> None:
    config = _wrangler_config()

    assert config["d1_databases"] == [
        {
            "binding": "DB",
            "database_name": "retro-backend",
            "database_id": "00000000-0000-0000-0000-000000000000",
            "migrations_dir": "migrations",
        }
    ]
    assert config["r2_buckets"] == [
        {"binding": "RAW_BUCKET", "bucket_name": "retro-gameplay-raw"}
    ]
    assert config["queues"] == {
        "producers": [
            {
                "binding": "GAMEPLAY_QUEUE",
                "queue": "retro-gameplay-events",
            },
            {
                "binding": "GAMEPLAY_DLQ",
                "queue": "retro-gameplay-events-dlq",
            },
            {
                "binding": "GAMEPLAY_ARCHIVE_DLQ",
                "queue": "retro-gameplay-events-archive-dlq",
            },
            {
                "binding": "EPISODE_QUEUE",
                "queue": "retro-episode-events",
            },
            {
                "binding": "EPISODE_DLQ",
                "queue": "retro-episode-events-dlq",
            },
        ],
        "consumers": [
            {
                "queue": "retro-gameplay-events",
                "max_batch_size": 10,
                "max_batch_timeout": 5,
                "max_retries": 5,
                "dead_letter_queue": "retro-gameplay-events-dlq",
            },
            {
                "queue": "retro-gameplay-events-dlq",
                "max_batch_size": 10,
                "max_batch_timeout": 5,
                "max_retries": 10,
                "dead_letter_queue": "retro-gameplay-events-archive-dlq",
            },
            {
                "queue": "retro-episode-events",
                "max_batch_size": 10,
                "max_batch_timeout": 5,
                "max_retries": 5,
                "dead_letter_queue": "retro-episode-events-dlq",
            },
        ],
    }


def test_wrangler_uses_exact_sqlite_durable_object_exports() -> None:
    config = _wrangler_config()

    assert config["durable_objects"] == {
        "bindings": [
            {"name": "CHUNK_GATE", "class_name": "ChunkGate"},
            {"name": "GAME_ROOMS", "class_name": "GameRoom"},
            {"name": "EPISODE_ASSEMBLER", "class_name": "EpisodeAssembler"},
        ]
    }
    assert config["exports"] == {
        "ChunkGate": {"type": "durable-object", "storage": "sqlite"},
        "GameRoom": {"type": "durable-object", "storage": "sqlite"},
        "EpisodeAssembler": {"type": "durable-object", "storage": "sqlite"},
    }
    assert "migrations" not in config


def test_wrangler_binds_native_ingest_rate_limiter() -> None:
    config = _wrangler_config()

    assert config["ratelimits"] == [
        {
            "name": "INGEST_RATE_LIMITER",
            "namespace_id": "1001",
            "simple": {"limit": 120, "period": 60},
        }
    ]


def test_wrangler_contains_only_approved_nonsecret_vars() -> None:
    config = _wrangler_config()
    readme = _read(README_PATH)

    expected_vars = {
        "AUTH_CACHE_TTL_SECONDS": 60,
        "AUTH_SESSION_URL": "https://auth.runretroarcade.com/api/auth/get-session",
        "MAX_CHUNK_BYTES": 262_144,
        "MAX_RESULT_BYTES": 16_384,
        "CHUNK_DEDUP_TTL_MS": 172_800_000,
        "CHUNK_PUBLISHING_LEASE_MS": 30_000,
        "ENVIRONMENT": "production",
        "PUBLIC_GAME_URL": "https://gamepigeon.example",
        "ALLOWED_GAME_ORIGINS": "https://gamepigeon.example",
    }
    assert config["vars"] == expected_vars
    assert "`AUTH_CACHE_TTL_SECONDS=60`" in readme
    assert "`AUTH_SESSION_URL=https://auth.runretroarcade.com/api/auth/get-session`" in readme
    assert "`MAX_RESULT_BYTES=16384`" in readme
    assert "`ENVIRONMENT=production`" in readme
    assert "`PUBLIC_GAME_URL=https://gamepigeon.example`" in readme
    assert "`ALLOWED_GAME_ORIGINS=https://gamepigeon.example`" in readme
    assert "16 KiB" in readme
    assert not any(
        marker in key.casefold()
        for key in expected_vars
        for marker in ("secret", "token", "password", "key")
    )


def test_auth_cache_uses_d1_misses_without_caching_leaderboards_or_using_r2() -> None:
    readme = _read(README_PATH)
    design = _read(DESIGN_PATH)
    config = _wrangler_config()
    auth_cache = _read(AUTH_CACHE_PATH)
    results_api = _read(RESULTS_API_PATH)

    documentation = " ".join("\n".join((readme, design)).casefold().split())
    assert "auth cache miss" in documentation
    assert "d1" in documentation
    assert "leaderboard responses are not cached" in documentation
    assert "r2" in documentation
    assert "raw and derived gameplay" in documentation
    assert "worker-level caching remains disabled" in documentation
    assert "custom domain" in documentation

    assert "cache" not in config
    assert "RAW_BUCKET" not in auth_cache
    assert "R2" not in auth_cache
    assert "EdgeAuthCache" not in results_api
    assert "AUTH_CACHE" not in results_api


def test_deferred_analytics_and_billing_are_not_configured() -> None:
    config_source = _read(WRANGLER_PATH).casefold()
    source_paths = {
        path.relative_to(SOURCE_ROOT).as_posix().casefold()
        for path in SOURCE_ROOT.rglob("*.py")
    }

    for marker in ("posthog", "stripe", "analytics_queue", "billing_queue"):
        assert marker not in config_source
    assert "api/stripe.py" not in source_paths
    assert "consumers/analytics.py" not in source_paths
    assert "consumers/billing.py" not in source_paths
    assert "integrations/posthog.py" not in source_paths
    assert "integrations/stripe.py" not in source_paths


#: The one TypeScript file this Python backend is allowed to carry, and why.
#:
#: The fixed-workers v2 environment *is* the tui-gamepigeon TypeScript engine:
#: a scored episode has to be driven by that engine, in-process, or it is not
#: the same environment the score claims. This file is the stdin/stdout bridge
#: that drives it and hands the measurement events back to the Python grader.
#: It is a subprocess entry point executed by `bun`, never imported by the
#: worker and never bundled into it, which is what the rest of this contract
#: exists to prevent. Anything else with a `.ts` or `.js` suffix is still a
#: contract violation.
_TYPESCRIPT_EXEMPTIONS = {"scripts/fixed_workers_v2_bridge.ts"}


def test_backend_has_no_typescript_or_javascript_source() -> None:
    excluded_directories = {
        ".claude",
        ".git",
        ".venv",
        ".venv-workers",
        ".wrangler",
        ".pytest_cache",
        ".ruff_cache",
        "js-stubs",
        "node_modules",
        "python_modules",
    }
    generated_source = [
        relative
        for path in ROOT.rglob("*")
        if path.is_file()
        and path.suffix in {".js", ".ts"}
        and not any(part in excluded_directories for part in path.parts)
        and (relative := path.relative_to(ROOT).as_posix()) not in _TYPESCRIPT_EXEMPTIONS
    ]

    assert generated_source == []
    # An exemption that no longer names a real file is an exemption nobody is
    # checking: it would silently widen the contract the next time that path
    # is reused.
    assert all((ROOT / exemption).is_file() for exemption in _TYPESCRIPT_EXEMPTIONS)


def test_worker_lock_is_committed_while_generated_modules_are_ignored() -> None:
    ignored = {
        line.strip()
        for line in _read(GITIGNORE_PATH).splitlines()
        if line.strip() and not line.lstrip().startswith("#")
    }

    assert {
        ".venv-workers/",
        ".wrangler/",
        "python_modules/",
        "src/js-stubs/",
    } <= ignored
    assert "pylock.toml" not in ignored
    assert PYLOCK_PATH.is_file()


def test_worker_toolchain_and_pyodide_dependencies_are_reproducibly_pinned() -> None:
    project = tomllib.loads(_read(PYPROJECT_PATH))
    runtime_dependencies = project["project"]["dependencies"]
    development_dependencies = project["dependency-groups"]["dev"]

    assert "pydantic==2.10.6" in runtime_dependencies
    assert "idna==3.18" in runtime_dependencies
    assert "uv==0.10.9" in development_dependencies
    assert "workers-py==1.15.0" in development_dependencies
    assert "workers-runtime-sdk==1.6.3" in development_dependencies

    uv_lock = tomllib.loads(_read(UV_LOCK_PATH))
    application_package = next(
        package
        for package in uv_lock["package"]
        if package["name"] == "retro-backend"
    )
    assert {"name": "idna"} in application_package["dependencies"]
    assert {
        "name": "idna",
        "specifier": "==3.18",
    } in application_package["metadata"]["requires-dist"]

    worker_lock = tomllib.loads(_read(PYLOCK_PATH))
    locked_packages = {
        package["name"]: package for package in worker_lock["packages"]
    }
    assert locked_packages["pydantic"]["version"] == "2.10.6"
    assert locked_packages["pydantic-core"]["version"] == "2.27.2"
    assert locked_packages["idna"]["version"] == "3.18"
    assert locked_packages["workers-runtime-sdk"]["version"] == "1.6.3"
    assert any(
        "pydantic_core-2.27.2-cp313-cp313-pyodide_2025_0_wasm32.whl"
        in wheel["url"]
        for wheel in locked_packages["pydantic-core"]["wheels"]
    )


def test_documented_worker_commands_use_the_frozen_project_toolchain() -> None:
    readme = _read(README_PATH)
    makefile = _read(MAKEFILE_PATH)

    assert _read(NODE_VERSION_PATH).strip() == "22.23.1"
    assert NODE_PREFLIGHT_PATH.is_file()
    assert DEPLOYMENT_PREFLIGHT_PATH.is_file()
    assert "Node.js 22.x" in readme
    assert "22.23.1" in readme
    assert "Node.js 23 and newer are unsupported" in readme
    assert "Node.js 22 or newer" not in readme
    assert "make node-preflight" in readme
    assert "make worker-sync" in readme
    assert "make worker-types" in readme
    assert "make worker-dry-run" in readme
    assert "make migrate-local" in readme
    assert "make migrate-remote" in readme
    assert "make deployment-preflight" in readme
    assert "make apply-r2-lifecycle" in readme
    assert "uv run --frozen uv --version" in readme
    assert "UV_RUN := uv run --frozen" in makefile
    assert "WRANGLER_VERSION ?= 4.112.0" in makefile
    assert "WRANGLER := npx --yes wrangler@$(WRANGLER_VERSION)" in makefile


def test_readme_documents_only_local_multiplayer_verification_commands() -> None:
    readme = _read(README_PATH)
    section = re.search(
        r"## Local Verification\s+```bash\n(?P<commands>.*?)\n```",
        readme,
        flags=re.DOTALL,
    )

    assert section is not None
    assert section.group("commands").splitlines() == [
        "make verify",
        "make worker-sync",
        "make worker-types",
        "make worker-dry-run",
    ]


def test_readme_documents_implemented_multiplayer_contract_and_routes() -> None:
    readme = _read(README_PATH)
    normalized = " ".join(readme.split())
    expected_routes = {
        "`POST /v1/rooms`",
        "`GET /v1/rooms/{room_id}`",
        "`POST /v1/rooms/{room_id}/join`",
        "`POST /v1/rooms/{room_id}/tickets`",
        "`GET /v1/rooms/{room_id}/socket?ticket={ticket}`",
    }

    for route in expected_routes:
        assert route in readme
    for statement in (
        "exactly two players",
        "two independent boards that are visible to both players",
        "submit moves concurrently",
        "No-op moves consume nothing and return no error",
        "both boards are finished",
        "The higher score wins; equal scores are a draw",
        "accepted legal move changes only the actor's board",
        "only that board's RNG state and board version advance",
        "persists the action and new room version before broadcasting a snapshot "
        + "containing both boards",
        "one-time WebSocket ticket",
    ):
        assert statement in normalized


def test_readme_documents_multiplayer_configuration_and_delivery_boundary() -> None:
    readme = _read(README_PATH)
    normalized = " ".join(readme.split())

    assert "`GAME_ROOMS` creates one SQLite Durable Object per room" in normalized
    assert "Production value placeholders" in normalized
    assert "Local example" in normalized
    assert "No separate Durable Object migration command is required" in normalized
    assert "each deploy reconciles the declarative export" in normalized
    # The Worker is live; the README must not claim otherwise.
    assert "not evidence of a live" not in normalized
    assert "does not deploy it" not in normalized
    assert "workerd" not in readme.casefold()


def test_readme_keeps_the_template_inert_and_documents_protected_render_inputs() -> None:
    readme = _read(README_PATH)
    normalized = " ".join(readme.split())
    wrangler = _read(WRANGLER_PATH)

    assert "Leave the committed `wrangler.toml` template placeholders untouched" in normalized
    assert "Replace the all-zero `database_id` in `wrangler.toml`" not in normalized
    assert 'export CLOUDFLARE_D1_DATABASE_ID="<uuid-from-d1-create>"' in readme
    assert 'export PUBLIC_GAME_URL="<canonical-public-game-url>"' in readme
    assert (
        'export ALLOWED_GAME_ORIGINS="<comma-separated-canonical-origins>"'
        in readme
    )
    assert 'export PUBLIC_GAME_URL="https://play.example.com"' not in readme
    assert "deliberately invalid" in normalized
    assert "protected GitHub environment secret" in normalized
    assert "protected GitHub environment variables" in normalized
    assert "Keep this template placeholder untouched" in wrangler
    assert "Replace this valid placeholder" not in wrangler


def test_readme_orders_remote_migration_after_rendered_config_preflight() -> None:
    readme = _read(README_PATH)
    provisioning = readme.split("## Provisioning", maxsplit=1)[1].split(
        "## Local Development",
        maxsplit=1,
    )[0]
    runnable_make_commands = re.findall(
        r"^make [^\n]+$",
        provisioning,
        flags=re.MULTILINE,
    )

    assert "make migrate-remote" not in runnable_make_commands
    expected_order = [
        "make migrate-local",
        "make prepare-deployment-config",
        "make deployment-preflight DEPLOY_CONFIG=wrangler.production.toml",
        "make migrate-remote DEPLOY_CONFIG=wrangler.production.toml",
    ]
    command_positions = [
        runnable_make_commands.index(command) for command in expected_order
    ]
    assert command_positions == sorted(command_positions)


def test_release_make_targets_use_the_pinned_wrangler() -> None:
    for target in (
        "worker-dry-run",
        "dev",
        "deploy",
        "migrate-local",
        "migrate-remote",
        "apply-r2-lifecycle",
    ):
        result = subprocess.run(
            ["make", "--dry-run", target],
            cwd=ROOT,
            check=False,
            capture_output=True,
            text=True,
        )
        assert result.returncode == 0, result.stderr
        assert "npx --yes wrangler@4.112.0" in result.stdout


def test_worker_make_targets_run_node_preflight_first() -> None:
    for target in (
        "worker-sync",
        "worker-types",
        "worker-dry-run",
        "dev",
        "deploy",
        "migrate-local",
        "migrate-remote",
        "apply-r2-lifecycle",
    ):
        result = subprocess.run(
            ["make", "--dry-run", target],
            cwd=ROOT,
            check=False,
            capture_output=True,
            text=True,
        )
        assert result.returncode == 0, result.stderr
        commands = result.stdout.splitlines()
        preflight_index = commands.index(
            "uv run --frozen python scripts/node_preflight.py"
        )
        pywrangler_index = next(
            index
            for index, command in enumerate(commands)
            if "pywrangler" in command or "npx --yes wrangler@" in command
        )
        assert preflight_index < pywrangler_index


def test_live_make_targets_require_deployment_preflight() -> None:
    for target in ("deploy", "migrate-remote", "apply-r2-lifecycle"):
        result = subprocess.run(
            ["make", "--dry-run", target],
            cwd=ROOT,
            check=False,
            capture_output=True,
            text=True,
        )
        assert result.returncode == 0, result.stderr
        commands = result.stdout.splitlines()
        deployment_preflight_index = commands.index(
            "uv run --frozen python scripts/deployment_preflight.py --config wrangler.toml"
        )
        wrangler_index = next(
            index
            for index, command in enumerate(commands)
            if "npx --yes wrangler@" in command
        )
        assert deployment_preflight_index < wrangler_index


def test_ci_uses_pinned_toolchains_and_canonical_worker_verification() -> None:
    workflow = _read(CI_PATH)
    commands = _workflow_run_commands(workflow)

    assert re.search(r"actions/setup-python@v\d+", workflow)
    assert re.search(r"python-version:\s*[\"']?3\.13[\"']?", workflow)
    assert re.search(r"actions/setup-node@v\d+", workflow)
    assert re.search(r"node-version-file:\s*[\"']?\.node-version[\"']?", workflow)
    assert not re.search(r"^\s+node-version:\s*", workflow, flags=re.MULTILINE)

    expected_commands = {
        "uv sync --frozen",
        "make verify",
        "make worker-sync",
        "make worker-dry-run",
    }
    assert expected_commands <= set(commands)
    assert commands.index("make worker-sync") < commands.index("make worker-dry-run")
    assert "make deploy" not in commands
    assert all("--remote" not in command for command in commands)
    assert all(
        "pywrangler deploy" not in command or "--dry-run" in command
        for command in commands
    )


def test_manual_production_workflow_is_guarded_and_smoke_tests_the_custom_domain() -> None:
    workflow = _read(DEPLOY_PATH)
    commands = _workflow_run_commands(workflow)

    assert re.search(r"^\s*workflow_dispatch:\s*$", workflow, flags=re.MULTILINE)
    assert not re.search(r"^\s+push:\s*$", workflow, flags=re.MULTILINE)
    assert "needs: verify" in workflow
    assert "if: github.ref == 'refs/heads/main'" in workflow
    assert "environment: production" in workflow
    assert "CLOUDFLARE_API_TOKEN: ${{ secrets.CLOUDFLARE_API_TOKEN }}" in workflow
    assert "CLOUDFLARE_ACCOUNT_ID: ${{ secrets.CLOUDFLARE_ACCOUNT_ID }}" in workflow
    assert (
        "CLOUDFLARE_D1_DATABASE_ID: ${{ secrets.CLOUDFLARE_D1_DATABASE_ID }}"
        in workflow
    )
    assert "PUBLIC_GAME_URL: ${{ vars.PUBLIC_GAME_URL }}" in workflow
    assert "ALLOWED_GAME_ORIGINS: ${{ vars.ALLOWED_GAME_ORIGINS }}" in workflow

    expected_commands = [
        "uv sync --frozen",
        "make verify",
        "make prepare-deployment-config",
        "make worker-dry-run DEPLOY_CONFIG=wrangler.production.toml",
        "make migrate-remote DEPLOY_CONFIG=wrangler.production.toml",
        "make apply-r2-lifecycle DEPLOY_CONFIG=wrangler.production.toml",
        "make deploy DEPLOY_CONFIG=wrangler.production.toml",
        (
            "curl --fail --silent --show-error --retry 8 --retry-all-errors "
            "--retry-delay 5 https://api.runretroarcade.com/health/ready"
        ),
    ]
    previous_index = -1
    for expected in expected_commands:
        previous_index = commands.index(expected, previous_index + 1)


def test_r2_lifecycle_uses_the_shape_wrangler_accepts() -> None:
    """Wrangler rejects the S3-style {"Rules": [{"Days": ...}]} spelling.

    `r2 bucket lifecycle set --file` requires a lowercase "rules" array and
    reads every condition as an age in seconds, so a rule that looks correct
    in S3 terms silently never applies.
    """
    lifecycle = __import__("json").loads(_read(R2_LIFECYCLE_PATH))

    assert set(lifecycle) == {"rules"}
    for rule in lifecycle["rules"]:
        assert set(rule) <= {
            "id",
            "enabled",
            "conditions",
            "storageClassTransitions",
            "deleteObjectsTransition",
        }
        assert rule["enabled"] is True
        assert rule["conditions"]["prefix"]
        for transition in rule.get("storageClassTransitions", []):
            assert transition["storageClass"] == "InfrequentAccess"
            assert transition["condition"]["type"] == "Age"
            assert transition["condition"]["maxAge"] % 86_400 == 0


def test_r2_lifecycle_bounds_derived_artifacts_and_retains_raw_chunks() -> None:
    lifecycle = __import__("json").loads(_read(R2_LIFECYCLE_PATH))
    expiry_days = {
        rule["conditions"]["prefix"]: (
            rule["deleteObjectsTransition"]["condition"]["maxAge"] // 86_400
        )
        for rule in lifecycle["rules"]
    }

    # Derived artifacts are all reconstructible by replaying raw/.
    assert expiry_days["validated/"] == 7
    assert expiry_days["rejected/"] == 7
    assert expiry_days["dlq/"] == 7

    # raw/ is the immutable audit trail that makes re-export possible, so it
    # outlives the derived artifacts and cools to Infrequent Access first.
    assert expiry_days["raw/"] == 90
    (raw_rule,) = [
        rule for rule in lifecycle["rules"] if rule["conditions"]["prefix"] == "raw/"
    ]
    (transition,) = raw_rule["storageClassTransitions"]
    assert transition["condition"]["maxAge"] // 86_400 == 30
    assert transition["condition"]["maxAge"] < (
        raw_rule["deleteObjectsTransition"]["condition"]["maxAge"]
    )


def test_readme_lists_the_implemented_high_scores_route() -> None:
    readme = _read(README_PATH)
    results_api = _read(RESULTS_API_PATH)

    assert '"/v1/high-scores/{game_type}"' in results_api
    assert "`GET /v1/high-scores/{game_type}`" in readme
    assert "/v1/leaderboards/{game_type}" not in readme


def test_design_and_plan_mark_deferred_integrations_as_future_work() -> None:
    design = _read(DESIGN_PATH)
    plan = _read(PLAN_PATH)
    design_preamble = "\n".join(design.splitlines()[:15])
    plan_preamble = "\n".join(plan.splitlines()[:15])

    for preamble in (design_preamble, plan_preamble):
        assert "Scope update (2026-07-17)" in preamble
        assert "deferred by user direction" in preamble
        assert "future work" in preamble

    assert "## Task 4: PostHog Analytics And Stripe Billing (Deferred)" in plan
    task_4 = plan.split("## Task 4:", maxsplit=1)[1].split("## Task 5:", maxsplit=1)[0]
    assert "not part of the active implementation" in task_4
    assert "**Files:**" not in task_4
    assert "- [ ]" not in task_4


def test_deferred_integrations_have_no_schema_or_advertised_resources() -> None:
    migration = _read(ROOT / "migrations" / "0001_core.sql")
    readme = _read(README_PATH)
    active_docs = "\n".join((_read(DESIGN_PATH), _read(PLAN_PATH)))

    for marker in ("stripe", "posthog", "billing", "analytics"):
        assert marker not in migration.casefold()
    for marker in (
        "/v1/webhooks/stripe",
        "retro-analytics-events",
        "retro-billing-events",
        "ANALYTICS_QUEUE",
        "BILLING_QUEUE",
        "stripe_customers",
        "processed_stripe_events",
        "POSTHOG_API_KEY",
        "STRIPE_SECRET_KEY",
        "src/retro_backend/api/stripe.py",
        "src/retro_backend/integrations/posthog.py",
    ):
        assert marker not in active_docs
    assert "no active tables or persistence" in readme.casefold()


def test_legacy_infrastructure_is_absent() -> None:
    assert [path for path in LEGACY_PATHS if (ROOT / path).exists()] == []


def test_runtime_dependencies_and_imports_are_cloudflare_native() -> None:
    project = tomllib.loads(_read(ROOT / "pyproject.toml"))["project"]
    dependency_names = {
        re.split(r"[<>=!~\[]", dependency, maxsplit=1)[0]
        for dependency in project["dependencies"]
    }
    assert dependency_names == {"fastapi", "httpx", "idna", "pydantic"}

    forbidden = (
        "alembic",
        "docker",
        "kafka",
        "psycopg",
        "redis",
        "sqlalchemy",
        "uvicorn",
    )
    pyproject_source = _read(ROOT / "pyproject.toml").casefold()
    runtime_source = "\n".join(
        _read(path).casefold() for path in SOURCE_ROOT.rglob("*.py")
    )
    for marker in forbidden:
        assert marker not in pyproject_source
        assert marker not in runtime_source
