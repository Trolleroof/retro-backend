import importlib
import sys
from collections.abc import Iterator
from dataclasses import dataclass
from http import HTTPMethod
from types import ModuleType, SimpleNamespace
from typing import override

import pytest

from retro_backend.worker_http import (
    CHUNK_BODY_HARD_LIMIT,
    RESULT_BODY_HARD_LIMIT,
    fetch_with_body_limit,
)


@dataclass(frozen=True, slots=True)
class FakeReadResult:
    done: bool
    value: object | None = None


class FakeJsChunk:
    def __init__(self, data: bytes, *, declared_size: int | None = None) -> None:
        self.data = data
        self.byteLength = len(data) if declared_size is None else declared_size
        self.length = self.byteLength
        self.to_bytes_calls = 0

    def to_bytes(self) -> bytes:
        self.to_bytes_calls += 1
        return self.data


class FakeReader:
    def __init__(self, chunks: list[FakeJsChunk]) -> None:
        self.chunks = list(chunks)
        self.read_calls = 0
        self.cancel_calls: list[str] = []
        self.release_lock_calls = 0

    async def read(self) -> FakeReadResult:
        self.read_calls += 1
        if not self.chunks:
            return FakeReadResult(done=True)
        return FakeReadResult(done=False, value=self.chunks.pop(0))

    async def cancel(self, reason: str) -> None:
        self.cancel_calls.append(reason)

    def releaseLock(self) -> None:
        self.release_lock_calls += 1


class FakeStream:
    def __init__(self, chunks: list[FakeJsChunk]) -> None:
        self.reader = FakeReader(chunks)
        self.get_reader_calls = 0
        self.cancel_calls: list[str] = []

    def getReader(self) -> FakeReader:
        self.get_reader_calls += 1
        return self.reader

    async def cancel(self, reason: str) -> None:
        self.cancel_calls.append(reason)


class FailingCancelStream(FakeStream):
    @override
    async def cancel(self, reason: str) -> None:
        self.cancel_calls.append(reason)
        raise RuntimeError("client stream already closed")


class AsgiFetchSpy:
    def __init__(self) -> None:
        self.calls: list[tuple[object, object, object]] = []

    async def __call__(
        self,
        app: object,
        request: object,
        environment: object,
    ) -> object:
        self.calls.append((app, request, environment))
        return SimpleNamespace(status=200)


class RequestFactorySpy:
    def __init__(self) -> None:
        self.calls: list[tuple[object, bytes, list[tuple[str, str]]]] = []

    def __call__(
        self,
        request: object,
        body: bytes,
        headers: list[tuple[str, str]],
    ) -> object:
        self.calls.append((request, body, headers))
        return SimpleNamespace(rebuilt=True, body=body, headers=headers)


def _request(
    path: str,
    stream: FakeStream | None,
    *,
    content_length: str | None = None,
) -> SimpleNamespace:
    headers = {
        "authorization": "Bearer opaque",
        "content-type": "application/json",
        "x-request-id": "req_01",
    }
    if content_length is not None:
        headers["content-length"] = content_length
    return SimpleNamespace(
        url=f"https://retro.example{path}?source=test",
        method="POST" if stream is not None else "GET",
        headers=headers,
        body=stream,
    )


def _environment(**overrides: str) -> SimpleNamespace:
    values = {
        "MAX_CHUNK_BYTES": str(CHUNK_BODY_HARD_LIMIT),
        "MAX_RESULT_BYTES": str(RESULT_BODY_HARD_LIMIT),
    }
    values.update(overrides)
    return SimpleNamespace(**values)


async def _fetch(
    request: object,
    environment: object,
    *,
    spy: AsgiFetchSpy,
    request_factory: RequestFactorySpy,
    too_large_response: object,
) -> object:
    return await fetch_with_body_limit(
        request,
        environment,
        app="fastapi-app",
        asgi_fetch=spy,
        request_factory=request_factory,
        too_large_response=lambda: too_large_response,
    )


async def test_fetch_boundary_rejects_known_oversize_before_reading() -> None:
    stream = FakeStream([FakeJsChunk(b"must-not-be-read")])
    request = _request(
        "/v1/game-results",
        stream,
        content_length=str(RESULT_BODY_HARD_LIMIT + 1),
    )
    spy = AsgiFetchSpy()
    request_factory = RequestFactorySpy()
    rejection = SimpleNamespace(status=413)

    response = await _fetch(
        request,
        _environment(),
        spy=spy,
        request_factory=request_factory,
        too_large_response=rejection,
    )

    assert response is rejection
    assert spy.calls == []
    assert request_factory.calls == []
    assert stream.get_reader_calls == 0
    assert stream.cancel_calls == ["request body exceeds maximum size"]


async def test_fetch_boundary_returns_413_when_best_effort_cancel_fails() -> None:
    stream = FailingCancelStream([FakeJsChunk(b"must-not-be-read")])
    request = _request(
        "/v1/game-results",
        stream,
        content_length=str(RESULT_BODY_HARD_LIMIT + 1),
    )
    spy = AsgiFetchSpy()
    rejection = SimpleNamespace(status=413)

    response = await _fetch(
        request,
        _environment(),
        spy=spy,
        request_factory=RequestFactorySpy(),
        too_large_response=rejection,
    )

    assert response is rejection
    assert spy.calls == []
    assert stream.cancel_calls == ["request body exceeds maximum size"]


async def test_fetch_boundary_stops_before_converting_overflow_chunk_and_cancels() -> None:
    first = FakeJsChunk(b"abc")
    overflow = FakeJsChunk(b"x", declared_size=6)
    stream = FakeStream([first, overflow, FakeJsChunk(b"must-not-be-read")])
    request = _request("/v1/game-results", stream)
    spy = AsgiFetchSpy()
    request_factory = RequestFactorySpy()
    rejection = SimpleNamespace(status=413)

    response = await _fetch(
        request,
        _environment(MAX_RESULT_BYTES="8"),
        spy=spy,
        request_factory=request_factory,
        too_large_response=rejection,
    )

    assert response is rejection
    assert spy.calls == []
    assert request_factory.calls == []
    assert first.to_bytes_calls == 1
    assert overflow.to_bytes_calls == 0
    assert stream.reader.read_calls == 2
    assert stream.reader.cancel_calls == ["request body exceeds maximum size"]
    assert stream.reader.release_lock_calls == 1


async def test_fetch_boundary_rebuilds_exact_bounded_body_without_stale_length() -> None:
    chunks = [FakeJsChunk(b"abc"), FakeJsChunk(b"def")]
    stream = FakeStream(chunks)
    request = _request("/v1/game-results", stream, content_length="6")
    environment = _environment(MAX_RESULT_BYTES="6")
    spy = AsgiFetchSpy()
    request_factory = RequestFactorySpy()

    response = await _fetch(
        request,
        environment,
        spy=spy,
        request_factory=request_factory,
        too_large_response=SimpleNamespace(status=413),
    )

    assert getattr(response, "status", None) == 200
    assert len(request_factory.calls) == 1
    original, body, headers = request_factory.calls[0]
    assert original is request
    assert body == b"abcdef"
    assert headers == [
        ("authorization", "Bearer opaque"),
        ("content-type", "application/json"),
        ("x-request-id", "req_01"),
    ]
    rebuilt = spy.calls[0][1]
    assert spy.calls == [("fastapi-app", rebuilt, environment)]
    assert stream.reader.cancel_calls == []
    assert stream.reader.release_lock_calls == 1


async def test_fetch_boundary_passes_no_body_request_through_unchanged() -> None:
    request = _request("/health/live", None)
    environment = _environment()
    spy = AsgiFetchSpy()
    request_factory = RequestFactorySpy()

    response = await _fetch(
        request,
        environment,
        spy=spy,
        request_factory=request_factory,
        too_large_response=SimpleNamespace(status=413),
    )

    assert getattr(response, "status", None) == 200
    assert spy.calls == [("fastapi-app", request, environment)]
    assert request_factory.calls == []


async def test_fetch_boundary_unwraps_bodyless_extension_method_request() -> None:
    raw_request = SimpleNamespace(
        body=None,
        method="PROPFIND",
        url="https://retro.example/v1/resource",
    )

    class RuntimeRequest:
        def __init__(self, js_object: SimpleNamespace) -> None:
            self.js_object = js_object
            self.method_accesses = 0

        @property
        def body(self) -> object:
            return self.js_object.body

        @property
        def method(self) -> HTTPMethod:
            self.method_accesses += 1
            return HTTPMethod[self.js_object.method]

    request = RuntimeRequest(raw_request)
    environment = _environment()
    calls: list[tuple[object, object, object]] = []

    async def asgi_fetch(
        app: object,
        incoming: object,
        called_environment: object,
    ) -> SimpleNamespace:
        assert incoming is raw_request
        assert raw_request.method == "PROPFIND"
        calls.append((app, incoming, called_environment))
        return SimpleNamespace(status=200)

    request_factory = RequestFactorySpy()
    response = await fetch_with_body_limit(
        request,
        environment,
        app="fastapi-app",
        asgi_fetch=asgi_fetch,
        request_factory=request_factory,
        too_large_response=lambda: SimpleNamespace(status=413),
    )

    assert getattr(response, "status", None) == 200
    assert calls == [("fastapi-app", raw_request, environment)]
    assert request.method_accesses == 0
    assert request_factory.calls == []
    with pytest.raises(KeyError, match="PROPFIND"):
        _ = request.method


@pytest.mark.parametrize(
    ("path", "environment", "content_length"),
    [
        (
            "/v1/game-results",
            _environment(MAX_RESULT_BYTES="8"),
            "9",
        ),
        (
            "/v1/future-body-route",
            _environment(MAX_RESULT_BYTES="8"),
            "9",
        ),
        (
            "/v1/game-event-chunks",
            _environment(MAX_CHUNK_BYTES="32"),
            "33",
        ),
        (
            "/v1/game-results",
            _environment(MAX_RESULT_BYTES="999999999"),
            str(RESULT_BODY_HARD_LIMIT + 1),
        ),
        (
            "/v1/game-event-chunks",
            _environment(MAX_CHUNK_BYTES="999999999"),
            str(CHUNK_BODY_HARD_LIMIT + 1),
        ),
    ],
)
async def test_fetch_boundary_enforces_configured_and_hard_route_caps(
    path: str,
    environment: SimpleNamespace,
    content_length: str,
) -> None:
    stream = FakeStream([FakeJsChunk(b"must-not-be-read")])
    request = _request(path, stream, content_length=content_length)
    spy = AsgiFetchSpy()
    request_factory = RequestFactorySpy()
    rejection = SimpleNamespace(status=413)

    response = await _fetch(
        request,
        environment,
        spy=spy,
        request_factory=request_factory,
        too_large_response=rejection,
    )

    assert response is rejection
    assert spy.calls == []
    assert stream.get_reader_calls == 0


async def test_chunk_route_accepts_body_larger_than_unknown_route_cap() -> None:
    payload = b"x" * (RESULT_BODY_HARD_LIMIT + 1)
    stream = FakeStream([FakeJsChunk(payload)])
    request = _request(
        "/v1/game-event-chunks",
        stream,
        content_length=str(len(payload)),
    )
    environment = _environment()
    spy = AsgiFetchSpy()
    request_factory = RequestFactorySpy()

    response = await _fetch(
        request,
        environment,
        spy=spy,
        request_factory=request_factory,
        too_large_response=SimpleNamespace(status=413),
    )

    assert getattr(response, "status", None) == 200
    assert request_factory.calls[0][1] == payload
    assert len(spy.calls) == 1


async def test_default_fetch_unwraps_before_reading_extension_method(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class RuntimeBlob:
        def __init__(self, body: bytes) -> None:
            self.js_object = body

    class RuntimeJsHeaders:
        def __init__(self, items: list[tuple[str, str]]) -> None:
            self._items = items

        def __iter__(self) -> Iterator[tuple[str, str]]:
            return iter(self._items)

    class RuntimeJsRequest:
        def __init__(
            self,
            url: str,
            *,
            method: str,
            headers: list[tuple[str, str]],
            body: object,
            redirect: str,
        ) -> None:
            self.url = url
            self.method = method
            self.headers = RuntimeJsHeaders(headers)
            self.body = body
            self.redirect = redirect

    runtime_request_wrappers: list[object] = []

    class RuntimeRequest:
        def __init__(
            self,
            input_request: RuntimeJsRequest | str,
            *,
            method: object | None = None,
            headers: list[tuple[str, str]] | None = None,
            body: object | None = None,
            redirect: str | None = None,
        ) -> None:
            self.method_accesses = 0
            if isinstance(input_request, RuntimeJsRequest):
                if any(option is not None for option in (method, headers, body, redirect)):
                    raise ValueError("raw JS requests do not accept constructor options")
                self.js_object = input_request
                runtime_request_wrappers.append(self)
                return

            assert method is not None
            assert headers is not None
            assert redirect is not None
            js_method = method.value if isinstance(method, HTTPMethod) else str(method)
            self.js_object = RuntimeJsRequest(
                input_request,
                method=js_method,
                headers=headers,
                body=body,
                redirect=redirect,
            )
            runtime_request_wrappers.append(self)

        @property
        def method(self) -> HTTPMethod:
            self.method_accesses += 1
            return HTTPMethod[self.js_object.method]

        @property
        def url(self) -> str:
            return self.js_object.url

        @property
        def headers(self) -> RuntimeJsHeaders:
            return self.js_object.headers

        @property
        def body(self) -> object:
            return self.js_object.body

        @property
        def redirect(self) -> str:
            return self.js_object.redirect

    class RuntimeResponse:
        @staticmethod
        def from_json(
            data: object,
            *,
            status: int,
        ) -> SimpleNamespace:
            return SimpleNamespace(data=data, status=status)

    class RuntimeWorkerEntrypoint:
        def __init__(self, environment: object) -> None:
            self.env = environment

    asgi_calls: list[tuple[object, RuntimeJsRequest, object]] = []

    async def asgi_spy(
        app: object,
        request: RuntimeJsRequest,
        environment: object,
    ) -> SimpleNamespace:
        assert request.method == "PROPFIND"
        asgi_calls.append((app, request, environment))
        return SimpleNamespace(status=200)

    fake_asgi = ModuleType("asgi")
    fake_asgi.__dict__["fetch"] = asgi_spy
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
        payload = b'{"score":1}'
        stream = FakeStream([FakeJsChunk(payload)])
        request_values = _request(
            "/v1/game-results",
            stream,
            content_length=str(len(payload)),
        )
        request = RuntimeRequest(
            RuntimeJsRequest(
                request_values.url,
                method="PROPFIND",
                headers=list(request_values.headers.items()),
                body=stream,
                redirect="manual",
            )
        )
        environment = _environment()

        response = await entry.Default(environment).fetch(request)

        assert getattr(response, "status", None) == 200
        assert len(asgi_calls) == 1
        app, rebuilt, called_environment = asgi_calls[0]
        assert isinstance(rebuilt, RuntimeJsRequest)
        assert len(runtime_request_wrappers) == 2
        incoming_wrapper, bounded_wrapper = runtime_request_wrappers
        assert incoming_wrapper is request
        assert isinstance(bounded_wrapper, RuntimeRequest)
        assert bounded_wrapper.js_object is rebuilt
        assert request.method_accesses == 0
        with pytest.raises(KeyError, match="PROPFIND"):
            _ = request.method
        assert app is entry.app
        assert called_environment is environment
        assert rebuilt.url == request.url
        assert rebuilt.method == "PROPFIND"
        assert rebuilt.body == payload
        assert rebuilt.redirect == "manual"
        rebuilt_headers = list(rebuilt.headers)
        assert ("content-length", str(len(payload))) not in rebuilt_headers
        assert ("authorization", "Bearer opaque") in rebuilt_headers
    finally:
        sys.modules.pop("retro_backend.entry", None)
        if previous_entry is not None:
            sys.modules["retro_backend.entry"] = previous_entry
