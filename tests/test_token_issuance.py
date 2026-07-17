import hashlib

import pytest
from fakeredis.aioredis import FakeRedis
from redis.exceptions import RedisError
from sqlalchemy import event
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker
from sqlalchemy.orm import Session

from retro_backend.auth_cache import (
    NEGATIVE_CACHE_VALUE,
    POSITIVE_AUTH_TTL_SECONDS,
    ROTATION_TOMBSTONE_TTL_SECONDS,
)
from retro_backend.models import User
from retro_backend.token_cli import (
    TokenAlreadyIssuedError,
    TokenRotationError,
    issue_token,
)


async def test_issue_token_stores_only_sha256_hash(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    token = await issue_token(session_factory, "usr_alice")

    assert token.startswith("retro_")
    assert len(token) >= 48
    async with session_factory() as session:
        user = await session.get(User, "usr_alice")
        assert user is not None
        assert user.auth_token_hash == hashlib.sha256(token.encode()).hexdigest()
        assert token not in user.auth_token_hash


@pytest.mark.parametrize(
    "user_id",
    ["", "alice", "usr_", "usr_contains space", "usr_slash/name", "usr_nonascii_\u00e9"],
)
async def test_issue_token_rejects_user_ids_outside_v2_grammar(
    session_factory: async_sessionmaker[AsyncSession], user_id: str
) -> None:
    with pytest.raises(ValueError, match="user ID has an invalid format"):
        await issue_token(session_factory, user_id)


async def test_issue_token_accepts_full_v2_user_id_grammar(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    token = await issue_token(session_factory, "usr_Alice.1-test_ok")

    assert token.startswith("retro_")
    async with session_factory() as session:
        assert await session.get(User, "usr_Alice.1-test_ok") is not None


async def test_issue_token_requires_rotate_for_existing_user(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    redis = FakeRedis(decode_responses=True)
    original = await issue_token(session_factory, "usr_alice")

    with pytest.raises(TokenAlreadyIssuedError, match="already has a token"):
        await issue_token(session_factory, "usr_alice")

    rotated = await issue_token(session_factory, "usr_alice", rotate=True, redis=redis)
    assert rotated != original
    async with session_factory() as session:
        user = await session.get(User, "usr_alice")
        assert user is not None
        assert user.auth_token_hash == hashlib.sha256(rotated.encode()).hexdigest()


async def test_rotate_tombstones_old_positive_auth_cache(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    redis = FakeRedis(decode_responses=True)
    original = await issue_token(session_factory, "usr_alice", redis=redis)
    original_digest = hashlib.sha256(original.encode()).hexdigest()
    old_cache_key = f"retro:auth:{original_digest}"
    await redis.set(old_cache_key, "usr_alice", ex=900)

    await issue_token(session_factory, "usr_alice", rotate=True, redis=redis)

    tombstone = await redis.get(old_cache_key)
    assert isinstance(tombstone, str)
    assert tombstone.startswith(f"{NEGATIVE_CACHE_VALUE}:")
    ttl = await redis.ttl(old_cache_key)
    assert POSITIVE_AUTH_TTL_SECONDS < ttl <= ROTATION_TOMBSTONE_TTL_SECONDS


async def test_rotation_tombstone_covers_long_transaction_and_is_reasserted_after_commit(
    session_factory: async_sessionmaker[AsyncSession],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    redis = FakeRedis(decode_responses=True)
    original = await issue_token(session_factory, "usr_alice", redis=redis)
    original_digest = hashlib.sha256(original.encode()).hexdigest()
    old_cache_key = f"retro:auth:{original_digest}"
    await redis.set(old_cache_key, "usr_alice", ex=POSITIVE_AUTH_TTL_SECONDS)
    original_set = redis.set
    tombstone_observations: list[tuple[int | None, str, bool]] = []

    async def inspecting_set(
        name: str,
        value: str,
        *,
        ex: int | None = None,
        nx: bool = False,
    ) -> object:
        result = await original_set(name, value, ex=ex, nx=nx)
        if name == old_cache_key and value.startswith(f"{NEGATIVE_CACHE_VALUE}:"):
            positive_repopulated = bool(
                await original_set(
                    old_cache_key,
                    "usr_alice",
                    ex=POSITIVE_AUTH_TTL_SECONDS,
                    nx=True,
                )
            )
            async with session_factory() as session:
                user = await session.get(User, "usr_alice")
                assert user is not None
                tombstone_observations.append((ex, user.auth_token_hash, positive_repopulated))
        return result

    monkeypatch.setattr(redis, "set", inspecting_set)
    rotated = await issue_token(session_factory, "usr_alice", rotate=True, redis=redis)
    rotated_digest = hashlib.sha256(rotated.encode()).hexdigest()

    assert tombstone_observations == [
        (None, original_digest, False),
        (ROTATION_TOMBSTONE_TTL_SECONDS, rotated_digest, False),
    ]
    assert await redis.ttl(old_cache_key) > POSITIVE_AUTH_TTL_SECONDS


async def test_rotate_tombstones_old_cache_before_committing_new_hash(
    session_factory: async_sessionmaker[AsyncSession],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    redis = FakeRedis(decode_responses=True)
    original = await issue_token(session_factory, "usr_alice", redis=redis)
    original_digest = hashlib.sha256(original.encode()).hexdigest()
    old_cache_key = f"retro:auth:{original_digest}"
    await redis.set(old_cache_key, "usr_alice", ex=900)
    original_set = redis.set
    hashes_seen_when_tombstoned: list[str] = []

    async def inspecting_set(name: str, value: str, *, ex: int | None = None) -> object:
        if name == old_cache_key and value.startswith(f"{NEGATIVE_CACHE_VALUE}:"):
            async with session_factory() as session:
                user = await session.get(User, "usr_alice")
                assert user is not None
                hashes_seen_when_tombstoned.append(user.auth_token_hash)
        return await original_set(name, value, ex=ex)

    monkeypatch.setattr(redis, "set", inspecting_set)
    rotated = await issue_token(session_factory, "usr_alice", rotate=True, redis=redis)

    assert hashes_seen_when_tombstoned == [
        original_digest,
        hashlib.sha256(rotated.encode()).hexdigest(),
    ]
    assert await redis.get(old_cache_key) != "usr_alice"
    async with session_factory() as session:
        user = await session.get(User, "usr_alice")
        assert user is not None
        assert user.auth_token_hash == hashlib.sha256(rotated.encode()).hexdigest()


async def test_rotate_aborts_database_change_when_tombstone_write_fails(
    session_factory: async_sessionmaker[AsyncSession],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    redis = FakeRedis(decode_responses=True)
    original = await issue_token(session_factory, "usr_alice", redis=redis)
    original_digest = hashlib.sha256(original.encode()).hexdigest()

    async def failed_set(_name: str, _value: str, *, ex: int | None = None) -> None:
        _ = ex
        raise RedisError("Redis unavailable")

    monkeypatch.setattr(redis, "set", failed_set)
    with pytest.raises(RedisError, match="Redis unavailable"):
        await issue_token(session_factory, "usr_alice", rotate=True, redis=redis)

    async with session_factory() as session:
        user = await session.get(User, "usr_alice")
        assert user is not None
        assert user.auth_token_hash == original_digest


async def test_rotate_requires_redis_before_changing_database(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    original = await issue_token(session_factory, "usr_alice")
    original_digest = hashlib.sha256(original.encode()).hexdigest()

    with pytest.raises(TokenRotationError, match="Redis is required"):
        await issue_token(session_factory, "usr_alice", rotate=True)

    async with session_factory() as session:
        user = await session.get(User, "usr_alice")
        assert user is not None
        assert user.auth_token_hash == original_digest


async def test_rotate_removes_tombstone_when_database_commit_fails(
    session_factory: async_sessionmaker[AsyncSession],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    redis = FakeRedis(decode_responses=True)
    original = await issue_token(session_factory, "usr_alice", redis=redis)
    original_digest = hashlib.sha256(original.encode()).hexdigest()
    old_cache_key = f"retro:auth:{original_digest}"
    await redis.set(old_cache_key, "usr_alice", ex=900)
    original_set = redis.set
    tombstones: list[str] = []

    async def recording_set(name: str, value: str, *, ex: int | None = None) -> object:
        if name == old_cache_key and value.startswith(f"{NEGATIVE_CACHE_VALUE}:"):
            tombstones.append(value)
        return await original_set(name, value, ex=ex)

    def fail_commit(_session: Session) -> None:
        raise RuntimeError("database commit failed")

    monkeypatch.setattr(redis, "set", recording_set)
    event.listen(Session, "before_commit", fail_commit)
    try:
        with pytest.raises(RuntimeError, match="database commit failed"):
            await issue_token(session_factory, "usr_alice", rotate=True, redis=redis)
    finally:
        event.remove(Session, "before_commit", fail_commit)

    assert len(tombstones) == 1
    assert await redis.get(old_cache_key) is None
    async with session_factory() as session:
        user = await session.get(User, "usr_alice")
        assert user is not None
        assert user.auth_token_hash == original_digest
