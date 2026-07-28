import hashlib
from dataclasses import dataclass
from types import SimpleNamespace

import pytest

from retro_backend.auth_cache import (
    AuthCacheEnvironment,
    EdgeAuthCache,
    build_auth_cache_environment,
)

TOKEN = "opaque-auth-token"
DIGEST = hashlib.sha256(TOKEN.encode("utf-8")).hexdigest()
REQUEST_URL = "https://api.example.com/v1/game-event-chunks?batch=1"
CACHE_URL = f"https://api.example.com/.retro-internal/auth/v1/{DIGEST}"


@dataclass(slots=True)
class FakeResponse:
    body: str
    ttl_seconds: int | None = None

    async def text(self) -> str:
        return self.body


class FakeCache:
    def __init__(self) -> None:
        self.values: dict[str, FakeResponse] = {}
        self.match_calls: list[str] = []
        self.put_calls: list[tuple[str, FakeResponse]] = []
        self.delete_calls: list[str] = []
        self.match_failure: Exception | None = None
        self.put_failure: Exception | None = None
        self.delete_failure: Exception | None = None

    async def match(self, key: object) -> object | None:
        assert isinstance(key, str)
        self.match_calls.append(key)
        if self.match_failure is not None:
            raise self.match_failure
        return self.values.get(key)

    async def put(self, key: object, response: object) -> None:
        assert isinstance(key, str)
        assert isinstance(response, FakeResponse)
        self.put_calls.append((key, response))
        if self.put_failure is not None:
            raise self.put_failure
        self.values[key] = response

    async def delete(self, key: object) -> bool:
        assert isinstance(key, str)
        self.delete_calls.append(key)
        if self.delete_failure is not None:
            raise self.delete_failure
        return self.values.pop(key, None) is not None


class BrokenCacheStorage:
    @property
    def default(self) -> object:
        raise RuntimeError("cache storage unavailable")


def _response_factory(body: str, ttl_seconds: int) -> FakeResponse:
    return FakeResponse(body=body, ttl_seconds=ttl_seconds)


def _cache(cache: FakeCache, *, ttl_seconds: int = 60) -> EdgeAuthCache:
    return EdgeAuthCache(
        cache,
        request_factory=lambda url: url,
        response_factory=_response_factory,
        ttl_seconds=ttl_seconds,
    )


def test_auth_cache_environment_delegates_bindings_without_mutating_source() -> None:
    source = SimpleNamespace(DB="database", RAW_BUCKET="bucket")
    cache = FakeCache()

    environment = AuthCacheEnvironment(source, _cache(cache))

    assert environment.DB == "database"
    assert environment.RAW_BUCKET == "bucket"
    assert environment.AUTH_CACHE is not None
    assert not hasattr(source, "AUTH_CACHE")


@pytest.mark.parametrize("with_backing_cache", [False, True])
async def test_request_auth_cache_reuses_positive_resolution_across_route_paths(
    with_backing_cache: bool,
) -> None:
    source = SimpleNamespace(DB="database")
    cache_api = FakeCache()
    backing_cache = _cache(cache_api) if with_backing_cache else None
    environment = AuthCacheEnvironment(source, backing_cache)
    request_cache = environment.AUTH_CACHE

    assert await request_cache.get(REQUEST_URL, DIGEST) is None
    await request_cache.put(REQUEST_URL, DIGEST, "usr_example")
    assert (
        await request_cache.get(
            "https://api.example.com/v1/rooms",
            DIGEST,
        )
        == "usr_example"
    )

    assert cache_api.match_calls == ([CACHE_URL] if with_backing_cache else [])
    assert len(cache_api.put_calls) == (1 if with_backing_cache else 0)


def test_runtime_cache_setup_failure_returns_original_d1_environment() -> None:
    source = SimpleNamespace(DB="database", AUTH_CACHE_TTL_SECONDS="60")

    environment = build_auth_cache_environment(
        source,
        BrokenCacheStorage(),
        request_factory=lambda url: url,
        response_factory=_response_factory,
    )

    assert environment is source


async def test_cache_hit_uses_digest_only_same_origin_key() -> None:
    cache = FakeCache()
    cache.values[CACHE_URL] = FakeResponse(
        '{"schema_version":1,"user_id":"usr_example"}'
    )

    user_id = await _cache(cache).get(REQUEST_URL, DIGEST)

    assert user_id == "usr_example"
    assert cache.match_calls == [CACHE_URL]
    assert TOKEN not in cache.match_calls[0]
    assert "/v1/game-event-chunks" not in cache.match_calls[0]


async def test_cache_miss_returns_none() -> None:
    cache = FakeCache()

    assert await _cache(cache).get(REQUEST_URL, DIGEST) is None
    assert cache.match_calls == [CACHE_URL]


async def test_positive_resolution_is_written_with_bounded_ttl() -> None:
    cache = FakeCache()

    await _cache(cache, ttl_seconds=999).put(REQUEST_URL, DIGEST, "usr_example")

    assert len(cache.put_calls) == 1
    key, response = cache.put_calls[0]
    assert key == CACHE_URL
    assert response.body == '{"schema_version":1,"user_id":"usr_example"}'
    assert response.ttl_seconds == 300
    assert TOKEN not in key
    assert TOKEN not in response.body


@pytest.mark.parametrize(
    "body",
    [
        "not-json",
        "{}",
        '{"schema_version":true,"user_id":"usr_example"}',
        '{"schema_version":2,"user_id":"usr_example"}',
        '{"schema_version":1,"user_id":3}',
        '{"schema_version":1,"user_id":"usr_example","extra":true}',
    ],
)
async def test_malformed_cached_value_is_deleted_and_treated_as_miss(
    body: str,
) -> None:
    cache = FakeCache()
    cache.values[CACHE_URL] = FakeResponse(body)

    assert await _cache(cache).get(REQUEST_URL, DIGEST) is None
    assert cache.delete_calls == [CACHE_URL]


async def test_cache_api_failures_are_best_effort() -> None:
    read_failure = FakeCache()
    read_failure.match_failure = RuntimeError("cache unavailable")
    write_failure = FakeCache()
    write_failure.put_failure = RuntimeError("cache unavailable")

    assert await _cache(read_failure).get(REQUEST_URL, DIGEST) is None
    await _cache(write_failure).put(REQUEST_URL, DIGEST, "usr_example")


async def test_delete_failure_does_not_replace_cache_miss() -> None:
    cache = FakeCache()
    cache.values[CACHE_URL] = FakeResponse("not-json")
    cache.delete_failure = RuntimeError("cache unavailable")

    assert await _cache(cache).get(REQUEST_URL, DIGEST) is None


async def test_revocation_purges_the_digest_from_this_data_center() -> None:
    cache_api = FakeCache()
    cache = _cache(cache_api)
    await cache.put(REQUEST_URL, DIGEST, "usr_example")

    await cache.delete("https://api.example.com/v1/auth/logout", DIGEST)

    assert cache_api.delete_calls == [CACHE_URL]
    assert await cache.get(REQUEST_URL, DIGEST) is None


async def test_revocation_purge_failure_is_best_effort() -> None:
    cache_api = FakeCache()
    cache_api.delete_failure = RuntimeError("cache unavailable")

    await _cache(cache_api).delete(REQUEST_URL, DIGEST)

    assert cache_api.delete_calls == [CACHE_URL]


@pytest.mark.parametrize("with_backing_cache", [True, False])
async def test_request_auth_cache_drops_the_memo_on_revocation(
    with_backing_cache: bool,
) -> None:
    cache_api = FakeCache()
    backing_cache = _cache(cache_api) if with_backing_cache else None
    request_cache = AuthCacheEnvironment(
        SimpleNamespace(DB="database"), backing_cache
    ).AUTH_CACHE
    await request_cache.put(REQUEST_URL, DIGEST, "usr_example")

    await request_cache.delete(REQUEST_URL, DIGEST)

    assert await request_cache.get(REQUEST_URL, DIGEST) is None
    assert cache_api.delete_calls == ([CACHE_URL] if with_backing_cache else [])


@pytest.mark.parametrize(
    "digest",
    ["", "a" * 63, "A" * 64, "g" * 64],
)
async def test_cache_rejects_noncanonical_token_digest(digest: str) -> None:
    cache = _cache(FakeCache())

    with pytest.raises(ValueError, match="lowercase hexadecimal SHA-256"):
        await cache.get(REQUEST_URL, digest)
    with pytest.raises(ValueError, match="lowercase hexadecimal SHA-256"):
        await cache.put(REQUEST_URL, digest, "usr_example")
    with pytest.raises(ValueError, match="lowercase hexadecimal SHA-256"):
        await cache.delete(REQUEST_URL, digest)


@pytest.mark.parametrize(
    "request_url",
    ["", "/relative", "ftp://api.example.com/chunks", "https:///missing-host"],
)
async def test_cache_rejects_non_http_request_origin(request_url: str) -> None:
    with pytest.raises(ValueError, match="absolute HTTP URL"):
        await _cache(FakeCache()).get(request_url, DIGEST)
