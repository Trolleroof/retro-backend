"""Token revocation routes backing client logout."""

from contextlib import suppress
from typing import Annotated, Literal

from fastapi import APIRouter, Depends, Query, Request, status

from ..auth import (
    hash_token,
    parse_bearer_token,
    request_auth_cache,
    request_environment,
    request_repository,
    require_user_id,
)
from ..auth_cache import configured_auth_cache_ttl_seconds

router = APIRouter()

type LogoutScope = Literal["session", "all_devices"]


@router.post("/v1/auth/logout", status_code=status.HTTP_200_OK)
async def logout(
    request: Request,
    user_id: Annotated[str, Depends(require_user_id)],
    scope: Annotated[LogoutScope, Query()] = "session",
) -> dict[str, object]:
    """Revoke the presented token, or every token this user holds.

    D1 revocation is immediate and authoritative. The edge cache is purged only
    in the data center serving this request, so a token already cached elsewhere
    stays accepted until ``edge_cache_ttl_seconds`` elapses.
    """
    token_digest = hash_token(parse_bearer_token(request.headers.get("Authorization")))
    repository = request_repository(request)

    if scope == "all_devices":
        revoked_sessions = await repository.revoke_user_sessions(user_id, token_digest)
    else:
        revoked_sessions = int(await repository.revoke_session(token_digest))

    auth_cache = request_auth_cache(request)
    if auth_cache is not None:
        with suppress(Exception):
            await auth_cache.delete(str(request.url), token_digest)

    return {
        "revoked_sessions": revoked_sessions,
        "scope": scope,
        "edge_cache_ttl_seconds": configured_auth_cache_ttl_seconds(
            request_environment(request)
        ),
    }
