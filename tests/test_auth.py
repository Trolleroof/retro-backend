import hashlib
import logging

import pytest
from fakeredis.aioredis import FakeRedis
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from retro_backend.auth import resolve_user_id
from retro_backend.models import User
from retro_backend.token_cli import NEGATIVE_CACHE_VALUE


async def test_auth_cache_miss_queries_postgres_and_sets_positive_ttl(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    token = "retro_valid-secret"
    digest = hashlib.sha256(token.encode()).hexdigest()
    redis = FakeRedis(decode_responses=True)
    async with session_factory() as session:
        session.add(User(id="usr_alice", auth_token_hash=digest))
        await session.commit()
        assert await resolve_user_id(token, session, redis) == "usr_alice"

    key = f"retro:auth:{digest}"
    assert await redis.get(key) == "usr_alice"
    assert 895 <= await redis.ttl(key) <= 900


async def test_auth_negative_cache_avoids_later_database_lookup(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    token = "retro_not-issued-yet"
    digest = hashlib.sha256(token.encode()).hexdigest()
    redis = FakeRedis(decode_responses=True)
    async with session_factory() as session:
        assert await resolve_user_id(token, session, redis) is None
        session.add(User(id="usr_late", auth_token_hash=digest))
        await session.commit()
        assert await resolve_user_id(token, session, redis) is None

    key = f"retro:auth:{digest}"
    assert await redis.get(key) == "!"
    assert 25 <= await redis.ttl(key) <= 30


async def test_auth_cache_hit_returns_user_without_postgres(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    token = "retro_cached"
    digest = hashlib.sha256(token.encode()).hexdigest()
    redis = FakeRedis(decode_responses=True)
    await redis.set(f"retro:auth:{digest}", "usr_cached", ex=900)

    async with session_factory() as session:
        assert await resolve_user_id(token, session, redis) == "usr_cached"


async def test_authentication_does_not_log_sensitive_values(
    session_factory: async_sessionmaker[AsyncSession], caplog: pytest.LogCaptureFixture
) -> None:
    token = "retro_super-secret"
    digest = hashlib.sha256(token.encode()).hexdigest()
    key = f"retro:auth:{digest}"
    redis = FakeRedis(decode_responses=True)

    with caplog.at_level(logging.DEBUG, logger="retro_backend"):
        async with session_factory() as session:
            assert await resolve_user_id(token, session, redis) is None

    output = "\n".join(
        record.getMessage()
        for record in caplog.records
        if record.name == "retro_backend" or record.name.startswith("retro_backend.")
    )
    assert token not in output
    assert digest not in output
    assert key not in output


async def test_auth_cache_fill_cannot_overwrite_rotation_tombstone(
    session_factory: async_sessionmaker[AsyncSession],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    token = "retro_rotating"
    digest = hashlib.sha256(token.encode()).hexdigest()
    key = f"retro:auth:{digest}"
    redis = FakeRedis(decode_responses=True)
    original_set = redis.set
    tombstone_injected = False

    async def racing_set(
        name: str, value: str, *, ex: int | None = None, nx: bool = False
    ) -> object:
        nonlocal tombstone_injected
        if not tombstone_injected:
            tombstone_injected = True
            await original_set(name, NEGATIVE_CACHE_VALUE, ex=30)
        return await original_set(name, value, ex=ex, nx=nx)

    monkeypatch.setattr(redis, "set", racing_set)
    async with session_factory() as session:
        session.add(User(id="usr_alice", auth_token_hash=digest))
        await session.commit()
        assert await resolve_user_id(token, session, redis) is None

    assert await redis.get(key) == NEGATIVE_CACHE_VALUE
