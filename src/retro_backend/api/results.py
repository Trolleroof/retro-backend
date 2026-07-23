"""D1-backed game-result and leaderboard routes."""

from dataclasses import asdict
from datetime import UTC, datetime
from typing import Annotated, cast

from fastapi import APIRouter, Depends, HTTPException, Query, Request, status
from pydantic import ValidationError

from ..auth import request_environment, require_user_id
from ..bindings import D1Database, positive_int_field, value_field
from ..contracts import GameResultRequest, GameType
from ..storage.d1 import D1Repository
from .request_body import BodyTooLarge, read_limited_body

DEFAULT_MAX_RESULT_BYTES = 16_384

router = APIRouter()


def _utc_timestamp(value: datetime) -> str:
    return value.astimezone(UTC).isoformat(timespec="microseconds").replace("+00:00", "Z")


def _repository(request: Request) -> D1Repository:
    environment = request_environment(request)
    database = value_field(environment, "DB") if environment is not None else None
    if database is None:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Database binding unavailable",
        )
    return D1Repository(cast(D1Database, database))


@router.post("/v1/game-results", status_code=status.HTTP_200_OK)
async def submit_result(
    request: Request,
    user_id: Annotated[str, Depends(require_user_id)],
) -> dict[str, bool | int]:
    environment = request_environment(request)
    max_bytes = min(
        positive_int_field(
            environment,
            "MAX_RESULT_BYTES",
            DEFAULT_MAX_RESULT_BYTES,
        ),
        DEFAULT_MAX_RESULT_BYTES,
    )
    try:
        raw_body = await read_limited_body(request, max_bytes=max_bytes)
    except BodyTooLarge as error:
        raise HTTPException(
            status_code=status.HTTP_413_CONTENT_TOO_LARGE,
            detail=str(error),
        ) from error

    try:
        result = GameResultRequest.model_validate_json(raw_body)
    except ValidationError as error:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            detail=error.errors(
                include_url=False,
                include_context=False,
                include_input=False,
            ),
        ) from error

    upsert = await _repository(request).upsert_high_score(
        user_id=user_id,
        game_type=result.game_type,
        score=result.score,
        achieved_at=_utc_timestamp(result.completed_at),
    )
    return {
        "accepted": True,
        "personal_best": upsert.personal_best,
        "high_score_updated": upsert.updated,
    }


@router.get(
    "/v1/high-scores/{game_type}",
    dependencies=[Depends(require_user_id)],
)
async def leaderboard(
    request: Request,
    game_type: GameType,
    limit: Annotated[int, Query(ge=1, le=100)] = 100,
) -> dict[str, object]:
    entries = await _repository(request).leaderboard(game_type, limit=limit)
    return {
        "game_type": game_type,
        "high_scores": [asdict(entry) for entry in entries],
    }
