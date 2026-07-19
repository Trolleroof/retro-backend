"""Liveness, readiness, and current-user routes."""

from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Request, status

from ..auth import request_environment, require_user_id
from ..bindings import value_field

router = APIRouter()


@router.get("/health/live")
async def live() -> dict[str, str]:
    return {"status": "healthy"}


@router.get("/health/ready")
async def ready(request: Request) -> dict[str, str]:
    environment = request_environment(request)
    database = value_field(environment, "DB") if environment is not None else None
    if database is None:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Database binding unavailable",
        )
    return {"status": "ready"}


@router.get("/v1/me")
async def current_user(
    user_id: Annotated[str, Depends(require_user_id)],
) -> dict[str, str]:
    return {"user_id": user_id}
