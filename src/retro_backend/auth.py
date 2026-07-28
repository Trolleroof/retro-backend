"""Opaque bearer-token parsing and hashing."""

import hashlib
from contextlib import suppress
from typing import cast
from urllib.parse import urlsplit

import httpx
from fastapi import HTTPException, Request, status

from .auth_cache import AuthCache
from .bindings import D1Database, value_field
from .storage.d1 import D1Repository


class AuthServiceUnavailable(RuntimeError):
    pass


def hash_token(token: str) -> str:
    """Return the stable digest stored in D1 for an opaque bearer token."""
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def canonical_user_id(auth_user_id: str) -> str:
    return f"usr_{hashlib.sha256(auth_user_id.encode('utf-8')).hexdigest()[:32]}"


async def resolve_auth_session(token: str, session_url: str) -> str | None:
    parsed = urlsplit(session_url)
    local = parsed.scheme == "http" and parsed.hostname in {"127.0.0.1", "localhost"}
    if not (local or parsed.scheme == "https"):
        raise AuthServiceUnavailable("Auth session URL must use HTTPS")
    try:
        async with httpx.AsyncClient(timeout=5) as client:
            response = await client.get(
                session_url,
                headers={"Authorization": f"Bearer {token}"},
            )
    except httpx.HTTPError as error:
        raise AuthServiceUnavailable("Auth service unavailable") from error
    if response.status_code in {401, 403}:
        return None
    if response.status_code != 200:
        raise AuthServiceUnavailable("Auth service unavailable")
    try:
        payload = response.json()
    except ValueError as error:
        raise AuthServiceUnavailable("Auth service returned invalid JSON") from error
    user = payload.get("user") if isinstance(payload, dict) else None
    auth_user_id = user.get("id") if isinstance(user, dict) else None
    if not isinstance(auth_user_id, str) or not auth_user_id or len(auth_user_id) > 256:
        return None
    return canonical_user_id(auth_user_id)


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


def request_auth_cache(request: Request) -> AuthCache | None:
    """Return the request-local auth cache adapter, if the Worker injected one."""
    environment = request_environment(request)
    auth_cache_value = (
        value_field(environment, "AUTH_CACHE") if environment is not None else None
    )
    if auth_cache_value is None:
        return None
    return cast(AuthCache, auth_cache_value)


def request_repository(request: Request) -> D1Repository:
    """Return a D1 repository, or fail closed when the binding is missing."""
    environment = request_environment(request)
    database = value_field(environment, "DB") if environment is not None else None
    if database is None:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Database binding unavailable",
        )
    return D1Repository(cast(D1Database, database))


async def require_user_id(request: Request) -> str:
    """Resolve an authenticated user from edge cache, falling back to D1."""
    token = parse_bearer_token(request.headers.get("Authorization"))
    token_digest = hash_token(token)
    environment = request_environment(request)
    auth_cache = request_auth_cache(request)

    if auth_cache is not None:
        try:
            cached_user_id = await auth_cache.get(str(request.url), token_digest)
        except Exception:
            cached_user_id = None
        if cached_user_id is not None:
            return cached_user_id

    repository = request_repository(request)
    user_id = await repository.resolve_user_hash(token_digest)
    if user_id is None:
        session_url = value_field(environment, "AUTH_SESSION_URL")
        # A revoked digest must not be re-mirrored from the upstream auth
        # service, which may still consider the token valid.
        if (
            isinstance(session_url, str)
            and session_url
            and not await repository.is_token_revoked(token_digest)
        ):
            try:
                user_id = await resolve_auth_session(token, session_url)
            except AuthServiceUnavailable as error:
                raise HTTPException(
                    status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
                    detail=str(error),
                ) from error
            if user_id is not None:
                await repository.upsert_auth_session(user_id, token_digest)
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
