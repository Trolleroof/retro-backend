"""Bound Worker request bodies before the ASGI adapter buffers them."""

import inspect
import string
from collections.abc import Awaitable, Callable, Iterable, Mapping
from typing import Literal, Protocol, cast
from urllib.parse import urlsplit

from .api.request_body import BodyTooLarge, content_length_exceeds
from .bindings import is_binding_null, positive_int_field, value_field

RESULT_BODY_HARD_LIMIT = 16_384
CHUNK_BODY_HARD_LIMIT = 262_144
ROOM_CREATE_BODY_HARD_LIMIT = 4096
_CHUNK_PATH = "/v1/game-event-chunks"
_ROOMS_PATH = "/v1/rooms"
_ROOM_ACTIONS = frozenset({"join", "tickets"})
_TOO_LARGE_REASON = "request body exceeds maximum size"
_MAX_SAFE_INTEGER_TEXT = "9007199254740991"
_HEX_DIGITS = frozenset(string.hexdigits)
_DELIMITER_BYTES = {0x2F, 0x5C}

AsgiFetch = Callable[[object, object, object], Awaitable[object]]
RequestFactory = Callable[
    [object, bytes, list[tuple[str, str]]],
    object,
]
ResponseFactory = Callable[[], object]
type RoomMethod = Literal["GET", "POST"]


class RoomResponseFactory(Protocol):
    def __call__(
        self,
        request: object,
        environment: object,
        *,
        allowed_method: RoomMethod | None,
        invalid_request: bool,
    ) -> object | Awaitable[object | None] | None: ...


class _InvalidPathEncoding(ValueError):
    def __init__(self, decoded_prefix: str) -> None:
        super().__init__("invalid percent-encoded path")
        self.decoded_prefix = decoded_prefix


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


def request_header_items(request: object) -> list[tuple[str, str]]:
    """Read raw Worker headers without touching unsafe wrapper conveniences."""
    raw_request = getattr(request, "js_object", request)
    headers = value_field(raw_request, "headers", ())
    return _header_items(() if headers is None else headers)


def _content_length(headers: list[tuple[str, str]]) -> str | None:
    for key, value in headers:
        if key.casefold() == "content-length":
            return value
    return None


def _valid_room_content_length(value: str | None) -> bool:
    if value is None:
        return True
    if (
        not value
        or not value.isascii()
        or not value.isdecimal()
        or (len(value) > 1 and value.startswith("0"))
    ):
        return False
    if len(value) != len(_MAX_SAFE_INTEGER_TEXT):
        return len(value) < len(_MAX_SAFE_INTEGER_TEXT)
    return value <= _MAX_SAFE_INTEGER_TEXT


def _without_content_length(
    headers: list[tuple[str, str]],
) -> list[tuple[str, str]]:
    return [
        (key, value)
        for key, value in headers
        if key.casefold() != "content-length"
    ]


def _method_and_path(request: object) -> tuple[object, str]:
    raw_request = getattr(request, "js_object", request)
    url = value_field(raw_request, "url")
    return value_field(raw_request, "method"), urlsplit(str(url)).path


def _is_room_create(method: object, path: str) -> bool:
    return method == "POST" and path == _ROOMS_PATH


def _canonical_room_http_method(path: str) -> RoomMethod | None:
    if path == _ROOMS_PATH:
        return "POST"
    prefix = f"{_ROOMS_PATH}/"
    if not path.startswith(prefix):
        return None

    parts = path[len(prefix) :].split("/")
    if len(parts) not in {1, 2}:
        return None
    if not parts[0]:
        return None
    if len(parts) == 1:
        return "GET"
    return "POST" if parts[1] in _ROOM_ACTIONS else None


def _valid_utf8_prefix(value: bytearray) -> str:
    encoded = bytes(value)
    try:
        return encoded.decode("utf-8")
    except UnicodeDecodeError as error:
        return encoded[: error.start].decode("utf-8")


def _strict_percent_decode_path(path: str) -> str:
    decoded = bytearray()
    index = 0
    while index < len(path):
        character = path[index]
        if character == "%":
            if (
                index + 2 >= len(path)
                or path[index + 1] not in _HEX_DIGITS
                or path[index + 2] not in _HEX_DIGITS
            ):
                raise _InvalidPathEncoding(_valid_utf8_prefix(decoded))
            decoded.append(int(path[index + 1 : index + 3], 16))
            index += 3
            continue
        try:
            decoded.extend(character.encode("utf-8"))
        except UnicodeEncodeError as error:
            raise _InvalidPathEncoding(_valid_utf8_prefix(decoded)) from error
        index += 1

    try:
        return bytes(decoded).decode("utf-8")
    except UnicodeDecodeError as error:
        prefix = bytes(decoded[: error.start]).decode("utf-8")
        raise _InvalidPathEncoding(prefix) from error


def _normalize_room_delimiters(path: str) -> str:
    normalized: list[str] = []
    index = 0
    while index < len(path):
        if (
            path[index] == "%"
            and index + 2 < len(path)
            and path[index + 1] in _HEX_DIGITS
            and path[index + 2] in _HEX_DIGITS
            and int(path[index + 1 : index + 3], 16) in _DELIMITER_BYTES
        ):
            normalized.append("/")
            index += 3
            continue
        normalized.append("/" if path[index] == "\\" else path[index])
        index += 1
    return "".join(normalized)


def _is_room_surface(path: str) -> bool:
    normalized = _normalize_room_delimiters(path)
    return normalized == _ROOMS_PATH or normalized.startswith(f"{_ROOMS_PATH}/")


def _is_encoded_room_path_alias(path: str) -> bool:
    if "%" not in path:
        return False
    try:
        decoded = _strict_percent_decode_path(path)
    except _InvalidPathEncoding as error:
        normalized_prefix = _normalize_room_delimiters(error.decoded_prefix)
        return normalized_prefix.startswith(f"{_ROOMS_PATH}/")
    return _is_room_surface(decoded)


def is_room_http_surface(request: object) -> bool:
    """Return whether the raw request path belongs to the room HTTP surface."""
    _method, path = _method_and_path(request)
    return _is_room_surface(path) or _is_encoded_room_path_alias(path)


def _body_limit(
    path: str,
    environment: object,
    *,
    is_room_create: bool,
) -> int:
    if is_room_create:
        return ROOM_CREATE_BODY_HARD_LIMIT
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


async def _room_response(
    room_response_factory: RoomResponseFactory,
    request: object,
    environment: object,
    *,
    allowed_method: RoomMethod | None,
    invalid_request: bool,
) -> object | None:
    response = room_response_factory(
        request,
        environment,
        allowed_method=allowed_method,
        invalid_request=invalid_request,
    )
    if inspect.isawaitable(response):
        return await response
    return response


async def _too_large_response_for_request(
    allowed_method: RoomMethod | None,
    request: object,
    environment: object,
    too_large_response: ResponseFactory,
    room_response_factory: RoomResponseFactory | None,
) -> object:
    if allowed_method is not None and room_response_factory is not None:
        response = await _room_response(
            room_response_factory,
            request,
            environment,
            allowed_method=allowed_method,
            invalid_request=True,
        )
        if response is not None:
            return response
    return too_large_response()


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
    room_response_factory: RoomResponseFactory | None = None,
) -> object:
    """Bound and rebuild a body-bearing request before invoking ASGI."""
    method, path = _method_and_path(request)
    if _is_encoded_room_path_alias(path):
        if room_response_factory is not None:
            response = await _room_response(
                room_response_factory,
                request,
                environment,
                allowed_method=None,
                invalid_request=True,
            )
            if response is not None:
                return response
        return too_large_response()

    is_room_create = _is_room_create(method, path)
    allowed_room_method = _canonical_room_http_method(path)
    if (
        allowed_room_method is None
        and _is_room_surface(path)
        and room_response_factory is not None
    ):
        response = await _room_response(
            room_response_factory,
            request,
            environment,
            allowed_method=None,
            invalid_request=True,
        )
        if response is not None:
            return response
    if allowed_room_method is not None and room_response_factory is not None:
        response = await _room_response(
            room_response_factory,
            request,
            environment,
            allowed_method=allowed_room_method,
            invalid_request=False,
        )
        if response is not None:
            return response

    room_headers: list[tuple[str, str]] | None = None
    room_content_length: str | None = None
    if is_room_create:
        room_headers = request_header_items(request)
        room_content_length = _content_length(room_headers)
        if not _valid_room_content_length(room_content_length):
            return await _too_large_response_for_request(
                allowed_room_method,
                request,
                environment,
                too_large_response,
                room_response_factory,
            )

    stream = value_field(request, "body")
    if is_binding_null(stream):
        asgi_request = getattr(request, "js_object", request)
        return await asgi_fetch(app, asgi_request, environment)

    max_bytes = _body_limit(
        path,
        environment,
        is_room_create=is_room_create,
    )
    headers = room_headers if room_headers is not None else request_header_items(request)
    content_length = (
        room_content_length if room_headers is not None else _content_length(headers)
    )
    if content_length_exceeds(content_length, max_bytes):
        await _cancel(stream)
        return await _too_large_response_for_request(
            allowed_room_method,
            request,
            environment,
            too_large_response,
            room_response_factory,
        )

    try:
        raw_body = await _read_worker_stream(stream, max_bytes)
    except BodyTooLarge:
        return await _too_large_response_for_request(
            allowed_room_method,
            request,
            environment,
            too_large_response,
            room_response_factory,
        )
    except Exception:
        if not is_room_create:
            raise
        return await _too_large_response_for_request(
            allowed_room_method,
            request,
            environment,
            too_large_response,
            room_response_factory,
        )

    headers = _without_content_length(headers)
    bounded_request = request_factory(request, raw_body, headers)
    return await asgi_fetch(app, bounded_request, environment)
