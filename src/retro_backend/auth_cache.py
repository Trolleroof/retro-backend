"""Best-effort Cloudflare edge cache for positive auth resolutions."""

import json
import re
from collections.abc import Callable, Mapping
from typing import Protocol, cast
from urllib.parse import urlsplit, urlunsplit

from .bindings import is_binding_null, positive_int_field

DEFAULT_AUTH_CACHE_TTL_SECONDS = 60
MAX_AUTH_CACHE_TTL_SECONDS = 300

_CACHE_SCHEMA_VERSION = 1
_CACHE_PATH_PREFIX = "/.retro-internal/auth/v1"
_SHA256_PATTERN = re.compile(r"^[0-9a-f]{64}$")


class AuthCache(Protocol):
    async def get(self, request_url: str, token_digest: str) -> str | None: ...

    async def put(
        self,
        request_url: str,
        token_digest: str,
        user_id: str,
    ) -> None: ...

    async def delete(self, request_url: str, token_digest: str) -> None: ...


class CacheApi(Protocol):
    async def match(self, key: object) -> object | None: ...

    async def put(self, key: object, response: object) -> object: ...

    async def delete(self, key: object) -> object: ...


class CacheStorage(Protocol):
    @property
    def default(self) -> object: ...


class CacheResponse(Protocol):
    async def text(self) -> str: ...


RequestFactory = Callable[[str], object]
ResponseFactory = Callable[[str, int], object]


class AuthCacheEnvironment:
    """Add one request-local auth cache while delegating Worker bindings."""

    __slots__ = ("_auth_cache", "_environment")

    def __init__(self, environment: object, auth_cache: AuthCache | None) -> None:
        self._environment = environment
        self._auth_cache = RequestAuthCache(auth_cache)

    @property
    def AUTH_CACHE(self) -> AuthCache:
        return self._auth_cache

    def __getattr__(self, name: str) -> object:
        if isinstance(self._environment, Mapping):
            try:
                return self._environment[name]
            except KeyError as error:
                raise AttributeError(name) from error
        return getattr(self._environment, name)


def _validate_digest(token_digest: str) -> None:
    if _SHA256_PATTERN.fullmatch(token_digest) is None:
        raise ValueError("token_digest must be lowercase hexadecimal SHA-256")


def _cache_url(request_url: str, token_digest: str) -> str:
    _validate_digest(token_digest)
    parsed = urlsplit(request_url)
    if parsed.scheme not in {"http", "https"} or not parsed.netloc:
        raise ValueError("request_url must be an absolute HTTP URL")
    return urlunsplit(
        (
            parsed.scheme,
            parsed.netloc,
            f"{_CACHE_PATH_PREFIX}/{token_digest}",
            "",
            "",
        )
    )


class RequestAuthCache:
    """Memoize positive auth resolutions for one Worker request."""

    def __init__(self, backing: AuthCache | None) -> None:
        self._backing = backing
        self._resolved: dict[str, str] = {}

    async def get(self, request_url: str, token_digest: str) -> str | None:
        cache_url = _cache_url(request_url, token_digest)
        cached_user_id = self._resolved.get(cache_url)
        if cached_user_id is not None:
            return cached_user_id
        if self._backing is None:
            return None

        user_id = await self._backing.get(request_url, token_digest)
        if user_id is not None:
            self._resolved[cache_url] = user_id
        return user_id

    async def put(
        self,
        request_url: str,
        token_digest: str,
        user_id: str,
    ) -> None:
        cache_url = _cache_url(request_url, token_digest)
        self._resolved[cache_url] = user_id
        if self._backing is not None:
            await self._backing.put(request_url, token_digest, user_id)

    async def delete(self, request_url: str, token_digest: str) -> None:
        cache_url = _cache_url(request_url, token_digest)
        _ = self._resolved.pop(cache_url, None)
        if self._backing is not None:
            await self._backing.delete(request_url, token_digest)


class EdgeAuthCache:
    """Store short-lived positive token resolutions in one edge data center."""

    def __init__(
        self,
        cache: CacheApi,
        *,
        request_factory: RequestFactory,
        response_factory: ResponseFactory,
        ttl_seconds: int = DEFAULT_AUTH_CACHE_TTL_SECONDS,
    ) -> None:
        if type(ttl_seconds) is not int:
            raise TypeError("ttl_seconds must be an integer")
        if ttl_seconds <= 0:
            raise ValueError("ttl_seconds must be positive")
        self._cache = cache
        self._request_factory = request_factory
        self._response_factory = response_factory
        self._ttl_seconds = min(ttl_seconds, MAX_AUTH_CACHE_TTL_SECONDS)

    async def get(self, request_url: str, token_digest: str) -> str | None:
        cache_url = _cache_url(request_url, token_digest)
        try:
            key = self._request_factory(cache_url)
            response = await self._cache.match(key)
        except Exception:
            return None
        if is_binding_null(response):
            return None

        try:
            raw_value = await cast(CacheResponse, response).text()
            value = json.loads(raw_value)
            if (
                not isinstance(value, dict)
                or set(value) != {"schema_version", "user_id"}
                or type(value["schema_version"]) is not int
                or value["schema_version"] != _CACHE_SCHEMA_VERSION
                or not isinstance(value["user_id"], str)
            ):
                raise ValueError("invalid auth cache value")
        except Exception:
            await self._delete_best_effort(key)
            return None
        return value["user_id"]

    async def put(
        self,
        request_url: str,
        token_digest: str,
        user_id: str,
    ) -> None:
        cache_url = _cache_url(request_url, token_digest)
        value = json.dumps(
            {"schema_version": _CACHE_SCHEMA_VERSION, "user_id": user_id},
            separators=(",", ":"),
            sort_keys=True,
        )
        try:
            key = self._request_factory(cache_url)
            response = self._response_factory(value, self._ttl_seconds)
            await self._cache.put(key, response)
        except Exception:
            return

    async def delete(self, request_url: str, token_digest: str) -> None:
        """Purge one digest from this data center's cache; other colos keep it
        until the TTL expires, so revocation is only bounded, never instant."""
        cache_url = _cache_url(request_url, token_digest)
        try:
            key = self._request_factory(cache_url)
        except Exception:
            return
        await self._delete_best_effort(key)

    async def _delete_best_effort(self, key: object) -> None:
        try:
            await self._cache.delete(key)
        except Exception:
            return


def configured_auth_cache_ttl_seconds(environment: object) -> int:
    """Return the effective edge TTL, which bounds how long revocation lags."""
    ttl_seconds = positive_int_field(
        environment,
        "AUTH_CACHE_TTL_SECONDS",
        DEFAULT_AUTH_CACHE_TTL_SECONDS,
    )
    return min(ttl_seconds, MAX_AUTH_CACHE_TTL_SECONDS)


def build_auth_cache_environment(
    environment: object,
    cache_storage: object,
    *,
    request_factory: RequestFactory,
    response_factory: ResponseFactory,
) -> object:
    """Inject edge auth caching, or preserve the original D1-only environment."""
    try:
        default_cache = cast(CacheStorage, cache_storage).default
        if is_binding_null(default_cache):
            return environment
        ttl_seconds = positive_int_field(
            environment,
            "AUTH_CACHE_TTL_SECONDS",
            DEFAULT_AUTH_CACHE_TTL_SECONDS,
        )
        auth_cache = EdgeAuthCache(
            cast(CacheApi, default_cache),
            request_factory=request_factory,
            response_factory=response_factory,
            ttl_seconds=ttl_seconds,
        )
        return AuthCacheEnvironment(environment, auth_cache)
    except Exception:
        return environment
