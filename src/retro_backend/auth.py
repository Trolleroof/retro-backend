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
from .token_cli import hash_token

POSITIVE_AUTH_TTL_SECONDS = 900
NEGATIVE_AUTH_TTL_SECONDS = 30
NEGATIVE_CACHE_VALUE = "!"


async def resolve_user_id(token: str, session: AsyncSession, redis: Redis) -> str | None:
    digest = hash_token(token)
    cache_key = f"retro:auth:{digest}"
    cached = await redis.get(cache_key)
    if cached is not None:
        if isinstance(cached, bytes):
            cached = cached.decode("utf-8")
        return None if hmac.compare_digest(cached, NEGATIVE_CACHE_VALUE) else cached

    user_id = await session.scalar(select(User.id).where(User.auth_token_hash == digest))
    if user_id is None:
        await redis.set(cache_key, NEGATIVE_CACHE_VALUE, ex=NEGATIVE_AUTH_TTL_SECONDS)
        return None
    await redis.set(cache_key, user_id, ex=POSITIVE_AUTH_TTL_SECONDS)
    return user_id


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
