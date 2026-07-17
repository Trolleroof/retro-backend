import hashlib

import pytest
from fakeredis.aioredis import FakeRedis
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from retro_backend.models import User
from retro_backend.token_cli import TokenAlreadyIssuedError, issue_token


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


async def test_issue_token_requires_rotate_for_existing_user(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    original = await issue_token(session_factory, "usr_alice")

    with pytest.raises(TokenAlreadyIssuedError, match="already has a token"):
        await issue_token(session_factory, "usr_alice")

    rotated = await issue_token(session_factory, "usr_alice", rotate=True)
    assert rotated != original
    async with session_factory() as session:
        user = await session.get(User, "usr_alice")
        assert user is not None
        assert user.auth_token_hash == hashlib.sha256(rotated.encode()).hexdigest()


async def test_rotate_invalidates_old_positive_auth_cache(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    redis = FakeRedis(decode_responses=True)
    original = await issue_token(session_factory, "usr_alice", redis=redis)
    original_digest = hashlib.sha256(original.encode()).hexdigest()
    old_cache_key = f"retro:auth:{original_digest}"
    await redis.set(old_cache_key, "usr_alice", ex=900)

    await issue_token(session_factory, "usr_alice", rotate=True, redis=redis)

    assert await redis.get(old_cache_key) is None
