"""Issue opaque bearer tokens for Retro users."""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import secrets
from collections.abc import Awaitable, Callable
from contextlib import suppress

from redis.asyncio import Redis
from redis.exceptions import RedisError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from .auth_cache import (
    ROTATION_TOMBSTONE_PREFIX,
    ROTATION_TOMBSTONE_RENEW_INTERVAL_SECONDS,
    ROTATION_TOMBSTONE_TTL_SECONDS,
)
from .config import get_settings
from .contracts import USER_ID
from .database import create_database_engine, create_session_factory
from .models import User
from .redis_client import create_redis

COMPARE_DELETE_SCRIPT = """
if redis.call('GET', KEYS[1]) == ARGV[1] then
  return redis.call('DEL', KEYS[1])
end
return 0
"""
COMPARE_EXPIRE_SCRIPT = """
if redis.call('GET', KEYS[1]) == ARGV[1] then
  return redis.call('EXPIRE', KEYS[1], ARGV[2])
end
return 0
"""


class TombstoneLease:
    def __init__(
        self,
        redis: Redis,
        key: str,
        value: str,
        *,
        ttl_seconds: int = ROTATION_TOMBSTONE_TTL_SECONDS,
        interval_seconds: float = ROTATION_TOMBSTONE_RENEW_INTERVAL_SECONDS,
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
    ) -> None:
        self._redis = redis
        self._key = key
        self._value = value
        self._ttl_seconds = ttl_seconds
        self._interval_seconds = interval_seconds
        self._sleep = sleep
        self._task: asyncio.Task[None] | None = None
        self._owner: asyncio.Task[object] | None = None
        self._failure: BaseException | None = None

    def start(self) -> None:
        if self._task is not None:
            raise RuntimeError("tombstone lease is already running")
        owner = asyncio.current_task()
        if owner is None:
            raise RuntimeError("tombstone lease requires an asyncio task")
        self._owner = owner
        self._task = asyncio.create_task(self._renew())

    async def _renew(self) -> None:
        try:
            while True:
                await self._sleep(self._interval_seconds)
                renewed = await self._redis.eval(
                    COMPARE_EXPIRE_SCRIPT,
                    1,
                    self._key,
                    self._value,
                    self._ttl_seconds,
                )
                if renewed != 1:
                    raise TokenRotationError("old-token tombstone lease was lost")
        except asyncio.CancelledError:
            raise
        except BaseException as error:
            self._failure = error
            if self._owner is not None and not self._owner.done():
                self._owner.cancel()

    async def stop(self) -> None:
        task = self._task
        if task is None:
            return
        task.cancel()
        with suppress(asyncio.CancelledError):
            await task
        self._task = None
        if self._failure is not None:
            raise self._failure


class TokenAlreadyIssuedError(RuntimeError):
    """Raised when rotation was not authorized for an existing user."""


class TokenRotationError(RuntimeError):
    """Raised when a token rotation cannot be completed safely."""


def hash_token(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def generate_token() -> str:
    return f"retro_{secrets.token_urlsafe(32)}"


async def _remove_tombstone_if_owned(redis: Redis, key: str, value: str) -> None:
    with suppress(RedisError):
        await redis.eval(COMPARE_DELETE_SCRIPT, 1, key, value)


async def issue_token(
    session_factory: async_sessionmaker[AsyncSession],
    user_id: str,
    *,
    rotate: bool = False,
    redis: Redis | None = None,
) -> str:
    if not USER_ID.fullmatch(user_id):
        raise ValueError("user ID has an invalid format")
    token = generate_token()
    tombstone_key: str | None = None
    tombstone_value: str | None = None
    tombstone_lease: TombstoneLease | None = None
    database_committed = False
    async with session_factory() as session:
        try:
            try:
                async with session.begin():
                    user = await session.get(User, user_id, with_for_update=True)
                    if user is not None and not rotate:
                        raise TokenAlreadyIssuedError(
                            f"user {user_id!r} already has a token; use --rotate"
                        )
                    digest = hash_token(token)
                    if user is None:
                        session.add(User(id=user_id, auth_token_hash=digest))
                    else:
                        if redis is None:
                            raise TokenRotationError("Redis is required for token rotation")
                        tombstone_key = f"retro:auth:{user.auth_token_hash}"
                        tombstone_value = (
                            f"{ROTATION_TOMBSTONE_PREFIX}{secrets.token_urlsafe(24)}"
                        )
                        installed = await redis.set(
                            tombstone_key,
                            tombstone_value,
                            ex=ROTATION_TOMBSTONE_TTL_SECONDS,
                        )
                        if not installed:
                            raise TokenRotationError("could not install old-token tombstone")
                        tombstone_lease = TombstoneLease(
                            redis,
                            tombstone_key,
                            tombstone_value,
                        )
                        tombstone_lease.start()
                        user.auth_token_hash = digest
                database_committed = True
            finally:
                if tombstone_lease is not None:
                    await tombstone_lease.stop()
            if redis is not None and tombstone_key is not None and tombstone_value is not None:
                reasserted = await redis.set(
                    tombstone_key,
                    tombstone_value,
                    ex=ROTATION_TOMBSTONE_TTL_SECONDS,
                )
                if not reasserted:
                    raise TokenRotationError("could not reassert old-token tombstone")
        except BaseException:
            if (
                not database_committed
                and redis is not None
                and tombstone_key is not None
                and tombstone_value is not None
            ):
                await _remove_tombstone_if_owned(redis, tombstone_key, tombstone_value)
            raise
    return token


async def _run(user_id: str, rotate: bool) -> str:
    settings = get_settings()
    engine = create_database_engine(settings)
    redis = create_redis(settings)
    try:
        return await issue_token(
            create_session_factory(engine),
            user_id,
            rotate=rotate,
            redis=redis,
        )
    finally:
        if redis is not None:
            await redis.aclose()
        await engine.dispose()


def main() -> None:
    parser = argparse.ArgumentParser(description="Issue a Retro bearer token")
    parser.add_argument("--user-id", required=True)
    parser.add_argument("--rotate", action="store_true")
    args = parser.parse_args()
    try:
        token = asyncio.run(_run(args.user_id, args.rotate))
    except TokenAlreadyIssuedError as error:
        parser.error(str(error))
    print(token)


if __name__ == "__main__":
    main()
