"""Bound Worker request bodies before the ASGI adapter buffers them."""

import inspect
from collections.abc import Awaitable, Callable, Iterable, Mapping
from typing import Protocol, cast
from urllib.parse import urlsplit

from .api.request_body import BodyTooLarge, content_length_exceeds
from .bindings import is_binding_null, positive_int_field, value_field

RESULT_BODY_HARD_LIMIT = 16_384
CHUNK_BODY_HARD_LIMIT = 262_144
_CHUNK_PATH = "/v1/game-event-chunks"
_TOO_LARGE_REASON = "request body exceeds maximum size"

AsgiFetch = Callable[[object, object, object], Awaitable[object]]
RequestFactory = Callable[
    [object, bytes, list[tuple[str, str]]],
    object,
]
ResponseFactory = Callable[[], object]


class _ByteLengthChunk(Protocol):
    byteLength: object


class _LengthChunk(Protocol):
    length: object


class _StreamReader(Protocol):
    async def read(self) -> object: ...


def _header_items(headers: object) -> list[tuple[str, str]]:
    if isinstance(headers, Mapping):
        source: Iterable[object] = headers.items()
    else:
        constructor = getattr(headers, "constructor", None)
        is_js_headers = getattr(constructor, "name", None) == "Headers"
        items = getattr(headers, "items", None)
        source = cast(
            Iterable[object],
            items() if callable(items) and not is_js_headers else headers,
        )

    result: list[tuple[str, str]] = []
    for item in source:
        key, value = cast(tuple[object, object], item)
        result.append((str(key), str(value)))
    return result


def _content_length(headers: list[tuple[str, str]]) -> str | None:
    for key, value in headers:
        if key.casefold() == "content-length":
            return value
    return None


def _without_content_length(
    headers: list[tuple[str, str]],
) -> list[tuple[str, str]]:
    return [
        (key, value)
        for key, value in headers
        if key.casefold() != "content-length"
    ]


def _body_limit(request: object, environment: object) -> int:
    url = value_field(request, "url")
    path = urlsplit(str(url)).path
    if path == _CHUNK_PATH:
        return min(
            positive_int_field(
                environment,
                "MAX_CHUNK_BYTES",
                CHUNK_BODY_HARD_LIMIT,
            ),
            CHUNK_BODY_HARD_LIMIT,
        )
    return min(
        positive_int_field(
            environment,
            "MAX_RESULT_BYTES",
            RESULT_BODY_HARD_LIMIT,
        ),
        RESULT_BODY_HARD_LIMIT,
    )


def _chunk_size(chunk: object) -> int | None:
    if hasattr(chunk, "byteLength"):
        size = cast(_ByteLengthChunk, chunk).byteLength
        if isinstance(size, int) and not isinstance(size, bool) and size >= 0:
            return size
    if hasattr(chunk, "length"):
        size = cast(_LengthChunk, chunk).length
        if isinstance(size, int) and not isinstance(size, bool) and size >= 0:
            return size
    if isinstance(chunk, bytes | bytearray | memoryview):
        return len(chunk)
    return None


def _chunk_bytes(chunk: object) -> bytes:
    if isinstance(chunk, bytes):
        return chunk
    if isinstance(chunk, bytearray | memoryview):
        return bytes(chunk)
    converter = getattr(chunk, "to_bytes", None)
    if not callable(converter):
        raise TypeError("Worker request body chunk is not byte-compatible")
    converted = converter()
    if not isinstance(converted, bytes):
        raise TypeError("Worker request body chunk did not convert to bytes")
    return converted


async def _cancel(target: object) -> None:
    cancel = getattr(target, "cancel", None)
    if not callable(cancel):
        return
    try:
        pending = cancel(_TOO_LARGE_REASON)
        if inspect.isawaitable(pending):
            await pending
    except Exception:
        # Cancellation is cleanup only; the bounded 413 response still wins.
        return


def _release_lock(reader: object) -> None:
    release = getattr(reader, "releaseLock", None)
    if not callable(release):
        return
    try:
        release()
    except Exception:
        return


async def _read_worker_stream(stream: object, max_bytes: int) -> bytes:
    get_reader = getattr(stream, "getReader", None)
    if not callable(get_reader):
        raise TypeError("Worker request body is not a readable stream")
    reader = cast(_StreamReader, get_reader())
    body = bytearray()
    try:
        while True:
            result = await reader.read()
            if value_field(result, "done") is True:
                break
            chunk = value_field(result, "value")
            if is_binding_null(chunk):
                raise TypeError("Worker request stream yielded an empty chunk value")

            remaining = max_bytes - len(body)
            declared_size = _chunk_size(chunk)
            if declared_size is not None and declared_size > remaining:
                await _cancel(reader)
                raise BodyTooLarge(_TOO_LARGE_REASON)

            chunk_bytes = _chunk_bytes(chunk)
            if len(chunk_bytes) > remaining:
                await _cancel(reader)
                raise BodyTooLarge(_TOO_LARGE_REASON)
            body.extend(chunk_bytes)
    finally:
        _release_lock(reader)
    return bytes(body)


async def fetch_with_body_limit(
    request: object,
    environment: object,
    *,
    app: object,
    asgi_fetch: AsgiFetch,
    request_factory: RequestFactory,
    too_large_response: ResponseFactory,
) -> object:
    """Bound and rebuild a body-bearing request before invoking ASGI."""
    stream = value_field(request, "body")
    if is_binding_null(stream):
        return await asgi_fetch(app, request, environment)

    max_bytes = _body_limit(request, environment)
    headers = _header_items(value_field(request, "headers"))
    if content_length_exceeds(_content_length(headers), max_bytes):
        await _cancel(stream)
        return too_large_response()

    try:
        raw_body = await _read_worker_stream(stream, max_bytes)
    except BodyTooLarge:
        return too_large_response()

    headers = _without_content_length(headers)
    bounded_request = request_factory(request, raw_body, headers)
    return await asgi_fetch(app, bounded_request, environment)
