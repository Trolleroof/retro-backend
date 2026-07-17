"""Health, event ingestion, and score routes."""

from __future__ import annotations

import json
from typing import Annotated, Any, Literal

from fastapi import APIRouter, Depends, HTTPException, Query, Request, status
from pydantic import AwareDatetime, BaseModel, ConfigDict, Field
from redis.asyncio import Redis
from redis.exceptions import RedisError
from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncSession

from ..auth import require_user_id
from ..contracts import ValidationError, validate_chunk_envelope
from ..database import get_session
from ..models import HighScore
from ..producer import EventProducer
from ..services.ingestion import BrokerPublishError, ChunkPublishingError, publish_chunk
from ..services.scores import upsert_personal_best

router = APIRouter()
GameType = Literal[
    "2048",
    "tetris",
    "snake",
    "minesweeper",
    "sokoban",
    "connect4",
    "lightsout",
    "sudoku",
]


class GameResultRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    result_id: str = Field(min_length=1)
    game_id: str = Field(min_length=1)
    game_type: GameType
    score: int = Field(ge=0, le=2**63 - 1, strict=True)
    completed_at: AwareDatetime


@router.get("/health/live")
async def live() -> dict[str, str]:
    return {"status": "healthy"}


@router.get("/health/ready")
async def ready(request: Request, session: AsyncSession = Depends(get_session)) -> dict[str, str]:
    await session.execute(text("SELECT 1"))
    redis: Redis | None = request.app.state.redis
    try:
        redis_ready = redis is not None and await redis.ping()
    except RedisError:
        redis_ready = False
    if not redis_ready:
        raise HTTPException(status_code=503, detail="redis unavailable")
    if request.app.state.producer is None:
        raise HTTPException(status_code=503, detail="event broker unavailable")
    return {"status": "ready"}


@router.post("/v1/game-event-chunks", status_code=status.HTTP_202_ACCEPTED)
async def ingest(
    request: Request,
    user_id: str = Depends(require_user_id),
) -> dict[str, Any]:
    settings = request.app.state.settings
    content_length = request.headers.get("content-length")
    if content_length is not None:
        try:
            if int(content_length) > settings.max_request_bytes:
                raise HTTPException(status_code=413, detail="payload too large")
        except ValueError as error:
            raise HTTPException(status_code=400, detail="invalid content-length") from error
    raw_payload = await request.body()
    if not raw_payload:
        raise HTTPException(status_code=400, detail="request body is required")
    if len(raw_payload) > settings.max_request_bytes:
        raise HTTPException(status_code=413, detail="payload too large")
    try:
        envelope = validate_chunk_envelope(json.loads(raw_payload))
    except (json.JSONDecodeError, UnicodeDecodeError, ValidationError) as error:
        raise HTTPException(status_code=400, detail=str(error)) from error

    redis: Redis | None = request.app.state.redis
    producer: EventProducer | None = request.app.state.producer
    if redis is None or producer is None:
        raise HTTPException(status_code=503, detail="event ingestion unavailable")
    try:
        result = await publish_chunk(
            redis=redis,
            producer=producer,
            settings=settings,
            user_id=user_id,
            envelope=envelope,
            raw_payload=raw_payload,
        )
    except ChunkPublishingError as error:
        raise HTTPException(status_code=503, detail=str(error)) from error
    except (BrokerPublishError, RedisError) as error:
        raise HTTPException(status_code=503, detail="event broker unavailable") from error
    return result.acknowledgment()


@router.post("/v1/game-results")
async def submit_game_result(
    result: GameResultRequest,
    user_id: str = Depends(require_user_id),
    session: AsyncSession = Depends(get_session),
) -> dict[str, Any]:
    try:
        personal_best, updated = await upsert_personal_best(
            session,
            user_id=user_id,
            game_type=result.game_type,
            score=result.score,
            achieved_at=result.completed_at,
        )
        await session.commit()
    except Exception:
        await session.rollback()
        raise
    return {
        "accepted": True,
        "personal_best": personal_best,
        "high_score_updated": updated,
    }


@router.get("/v1/high-scores/{game_type}")
async def get_high_scores(
    game_type: GameType,
    _user_id: str = Depends(require_user_id),
    session: AsyncSession = Depends(get_session),
    limit: Annotated[int, Query(ge=1, le=100)] = 100,
) -> dict[str, Any]:
    scores = list(
        (
            await session.scalars(
                select(HighScore)
                .where(HighScore.game_type == game_type)
                .order_by(HighScore.score.desc(), HighScore.achieved_at.asc())
                .limit(limit)
            )
        ).all()
    )
    return {
        "game_type": game_type,
        "high_scores": [
            {
                "user_id": score.user_id,
                "score": score.score,
                "achieved_at": score.achieved_at,
            }
            for score in scores
        ],
    }
