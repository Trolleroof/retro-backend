"""Redis-first bearer authentication backed by hashed PostgreSQL tokens."""

from __future__ import annotations

import hmac

from fastapi import Depends, Header, HTTPException, Request, status
from redis.asyncio import Redis
from redis.exceptions import RedisError
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from .database import get_session
from .models import User
from .token_cli import (
    NEGATIVE_AUTH_TTL_SECONDS,
    NEGATIVE_CACHE_VALUE,
    ROTATION_TOMBSTONE_PREFIX,
    hash_token,
)

POSITIVE_AUTH_TTL_SECONDS = 900


def _cached_user_id(cached: bytes | str | None) -> str | None:
    if isinstance(cached, bytes):
        cached = cached.decode("utf-8")
    if (
        cached is None
        or hmac.compare_digest(cached, NEGATIVE_CACHE_VALUE)
        or cached.startswith(ROTATION_TOMBSTONE_PREFIX)
    ):
        return None
    return cached


async def resolve_user_id(token: str, session: AsyncSession, redis: Redis) -> str | None:
    digest = hash_token(token)
    cache_key = f"retro:auth:{digest}"
    cached = await redis.get(cache_key)
    if cached is not None:
        return _cached_user_id(cached)

    user_id = await session.scalar(select(User.id).where(User.auth_token_hash == digest))
    cache_value = NEGATIVE_CACHE_VALUE if user_id is None else user_id
    ttl = NEGATIVE_AUTH_TTL_SECONDS if user_id is None else POSITIVE_AUTH_TTL_SECONDS
    stored = await redis.set(cache_key, cache_value, ex=ttl, nx=True)
    if stored:
        return user_id
    return _cached_user_id(await redis.get(cache_key))


async def require_user_id(
    request: Request,
    authorization: str | None = Header(default=None),
    session: AsyncSession = Depends(get_session),
) -> str:
    scheme, separator, token = (authorization or "").partition(" ")
    if not separator or not token or not hmac.compare_digest(scheme, "Bearer"):
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Missing bearer token")
    redis: Redis | None = request.app.state.redis
    if redis is None:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="authentication cache unavailable",
        )
    try:
        user_id = await resolve_user_id(token, session, redis)
    except RedisError as error:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="authentication cache unavailable",
        ) from error
    if user_id is None:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Invalid bearer token")
    return user_id
