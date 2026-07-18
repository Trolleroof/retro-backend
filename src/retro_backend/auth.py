"""Opaque bearer-token parsing and hashing."""

import hashlib
from contextlib import suppress
from typing import cast

from fastapi import HTTPException, Request, status

from .auth_cache import AuthCache
from .bindings import D1Database, value_field
from .storage.d1 import D1Repository


def hash_token(token: str) -> str:
    """Return the stable digest stored in D1 for an opaque bearer token."""
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def parse_bearer_token(authorization: str | None) -> str:
    """Parse exactly one token from the case-sensitive ``Bearer`` scheme."""
    if not authorization:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Missing bearer token",
            headers={"WWW-Authenticate": "Bearer"},
        )

    scheme, separator, token = authorization.partition(" ")
    if scheme != "Bearer" or separator != " " or not token or any(
        character.isspace() for character in token
    ):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid bearer token",
            headers={"WWW-Authenticate": "Bearer"},
        )
    return token


def request_environment(request: Request) -> object | None:
    """Return the Worker environment, with an app-state fallback for CPython tests."""
    environment = request.scope.get("env")
    if environment is not None:
        return environment
    return getattr(request.app.state, "env", None)


async def require_user_id(request: Request) -> str:
    """Resolve an authenticated user from edge cache, falling back to D1."""
    token = parse_bearer_token(request.headers.get("Authorization"))
    token_digest = hash_token(token)
    environment = request_environment(request)
    auth_cache_value = (
        value_field(environment, "AUTH_CACHE") if environment is not None else None
    )
    auth_cache = (
        cast(AuthCache, auth_cache_value) if auth_cache_value is not None else None
    )

    if auth_cache is not None:
        try:
            cached_user_id = await auth_cache.get(str(request.url), token_digest)
        except Exception:
            cached_user_id = None
        if cached_user_id is not None:
            return cached_user_id

    database = value_field(environment, "DB") if environment is not None else None
    if database is None:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Database binding unavailable",
        )

    user_id = await D1Repository(cast(D1Database, database)).resolve_user_hash(
        token_digest
    )
    if user_id is None:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid bearer token",
            headers={"WWW-Authenticate": "Bearer"},
        )

    if auth_cache is not None:
        with suppress(Exception):
            await auth_cache.put(str(request.url), token_digest, user_id)
    return user_id
