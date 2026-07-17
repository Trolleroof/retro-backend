"""Issue opaque bearer tokens for Retro users."""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import secrets

from redis.asyncio import Redis
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from .config import get_settings
from .database import create_database_engine, create_session_factory
from .models import User
from .redis_client import create_redis

NEGATIVE_AUTH_TTL_SECONDS = 30
NEGATIVE_CACHE_VALUE = "!"


class TokenAlreadyIssuedError(RuntimeError):
    """Raised when rotation was not authorized for an existing user."""


def hash_token(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def generate_token() -> str:
    return f"retro_{secrets.token_urlsafe(32)}"


async def issue_token(
    session_factory: async_sessionmaker[AsyncSession],
    user_id: str,
    *,
    rotate: bool = False,
    redis: Redis | None = None,
) -> str:
    if not user_id:
        raise ValueError("user ID must not be empty")
    token = generate_token()
    previous_digest: str | None = None
    async with session_factory() as session, session.begin():
        user = await session.get(User, user_id)
        if user is not None and not rotate:
            raise TokenAlreadyIssuedError(f"user {user_id!r} already has a token; use --rotate")
        digest = hash_token(token)
        if user is None:
            session.add(User(id=user_id, auth_token_hash=digest))
        else:
            previous_digest = user.auth_token_hash
            user.auth_token_hash = digest
    if previous_digest is not None and redis is not None:
        await redis.set(
            f"retro:auth:{previous_digest}",
            NEGATIVE_CACHE_VALUE,
            ex=NEGATIVE_AUTH_TTL_SECONDS,
        )
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
