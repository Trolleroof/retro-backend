"""Authenticated, bounded gameplay chunk ingestion route."""

import time
from typing import Annotated, cast

from fastapi import APIRouter, Depends, HTTPException, Request, status

from ..auth import request_environment, require_user_id
from ..bindings import (
    RateLimiterBinding,
    is_binding_null,
    positive_int_field,
    value_field,
)
from ..contracts import ContractError, IngestAcknowledgment
from ..services.ingestion import ChunkConflict, IngestionUnavailable, ingest_chunk

DEFAULT_MAX_CHUNK_BYTES = 262_144

router = APIRouter()


class BodyTooLarge(ValueError):
    """Raised as soon as a request body exceeds its configured bound."""


def _decimal_exceeds(value: str, limit: int) -> bool:
    if not value.isascii() or not value.isdecimal():
        return False
    normalized = value.lstrip("0") or "0"
    limit_text = str(limit)
    if len(normalized) != len(limit_text):
        return len(normalized) > len(limit_text)
    return normalized > limit_text


async def _read_limited_body(request: Request, *, max_bytes: int) -> bytes:
    content_length = request.headers.get("content-length")
    if content_length is not None and _decimal_exceeds(content_length, max_bytes):
        raise BodyTooLarge("request body exceeds maximum size")

    body = bytearray()
    async for chunk in request.stream():
        body.extend(chunk)
        if len(body) > max_bytes:
            raise BodyTooLarge("request body exceeds maximum size")
    return bytes(body)


def _required_environment(request: Request) -> object:
    environment = request_environment(request)
    if environment is None or any(
        is_binding_null(value_field(environment, name))
        for name in (
            "INGEST_RATE_LIMITER",
            "RAW_BUCKET",
            "GAMEPLAY_QUEUE",
            "CHUNK_GATE",
        )
    ):
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Event ingestion unavailable",
        )
    return environment


async def _enforce_rate_limit(environment: object, user_id: str) -> None:
    binding = value_field(environment, "INGEST_RATE_LIMITER")
    try:
        result = await cast(RateLimiterBinding, binding).limit(
            {"key": f"{user_id}:game-event-chunks"}
        )
    except Exception as error:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Event ingestion unavailable",
        ) from error

    success = value_field(result, "success")
    if success is False:
        raise HTTPException(
            status_code=status.HTTP_429_TOO_MANY_REQUESTS,
            detail="Rate limit exceeded",
            headers={"Retry-After": "60"},
        )
    if success is not True:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Event ingestion unavailable",
        )


@router.post(
    "/v1/game-event-chunks",
    response_model=IngestAcknowledgment,
    status_code=status.HTTP_202_ACCEPTED,
)
async def submit_chunk(
    request: Request,
    user_id: Annotated[str, Depends(require_user_id)],
) -> IngestAcknowledgment:
    environment = _required_environment(request)
    await _enforce_rate_limit(environment, user_id)
    max_bytes = positive_int_field(
        environment,
        "MAX_CHUNK_BYTES",
        DEFAULT_MAX_CHUNK_BYTES,
    )
    try:
        raw_body = await _read_limited_body(request, max_bytes=max_bytes)
    except BodyTooLarge as error:
        raise HTTPException(
            status_code=status.HTTP_413_CONTENT_TOO_LARGE,
            detail=str(error),
        ) from error
    if not raw_body:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="request body is empty",
        )

    try:
        return await ingest_chunk(
            environment,
            user_id,
            raw_body,
            time.time_ns() // 1_000_000,
        )
    except ContractError as error:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=str(error),
        ) from error
    except ChunkConflict as error:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=str(error),
        ) from error
    except IngestionUnavailable as error:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail=str(error),
        ) from error
