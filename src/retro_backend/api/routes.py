"""Health, ingestion, and game-query routes."""

import json
import logging
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Request, Response, status
from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncSession

from ..auth import require_api_key
from ..contracts import ValidationError
from ..database import get_session
from ..models import GameplayEvent, GameSession
from ..services.ingestion import ChunkConflictError, ingest_chunk

router = APIRouter()
logger = logging.getLogger(__name__)


@router.get("/health/live")
async def live() -> dict[str, str]:
    return {"status": "healthy"}


@router.get("/health/ready")
async def ready(request: Request, session: AsyncSession = Depends(get_session)) -> dict[str, str]:
    await session.execute(text("SELECT 1"))
    redis = request.app.state.redis
    if request.app.state.settings.redis_required and (redis is None or not await redis.ping()):
        raise HTTPException(status_code=503, detail="redis unavailable")
    return {"status": "ready"}


@router.post(
    "/v1/game-event-chunks",
    status_code=status.HTTP_202_ACCEPTED,
    dependencies=[Depends(require_api_key)],
)
async def ingest(request: Request, session: AsyncSession = Depends(get_session)) -> dict[str, Any]:
    raw_payload = await request.body()
    if not raw_payload:
        raise HTTPException(status_code=400, detail="request body is required")
    if len(raw_payload) > request.app.state.settings.max_request_bytes:
        raise HTTPException(status_code=413, detail="payload too large")
    try:
        result = await ingest_chunk(session, raw_payload)
        await session.commit()
    except (json.JSONDecodeError, UnicodeDecodeError, ValidationError) as error:
        await session.rollback()
        raise HTTPException(status_code=400, detail=str(error)) from error
    except ChunkConflictError as error:
        await session.rollback()
        raise HTTPException(status_code=409, detail=str(error)) from error
    redis = request.app.state.redis
    if redis is not None and not result.duplicate:
        try:
            await redis.set(
                f"retro:chunk:{result.chunk['chunk_id']}",
                "accepted",
                ex=48 * 60 * 60,
            )
            await redis.publish("retro:chunks:accepted", result.chunk["chunk_id"])
        except Exception:
            logger.warning("Redis notification failed after durable ingestion", exc_info=True)
    return result.acknowledgment()


@router.get("/v1/games/{game_id}", dependencies=[Depends(require_api_key)])
async def get_game(game_id: str, session: AsyncSession = Depends(get_session)) -> dict[str, Any]:
    game = await session.get(GameSession, game_id)
    if game is None:
        raise HTTPException(status_code=404, detail="game not found")
    return {
        "game_id": game.id,
        "user_id": game.user_id,
        "game_type": game.game_type,
        "game_version": game.game_version,
        "status": game.status,
        "completed": game.completed,
        "final_score": game.final_score,
        "first_event_sequence": game.first_event_sequence,
        "last_event_sequence": game.last_event_sequence,
        "event_count": game.event_count,
        "chunk_count": game.chunk_count,
    }


@router.get("/v1/games/{game_id}/events", dependencies=[Depends(require_api_key)])
async def get_events(
    game_id: str,
    response: Response,
    visibility: str | None = None,
    limit: int = 1000,
    session: AsyncSession = Depends(get_session),
) -> dict[str, Any]:
    if limit < 1 or limit > 5000:
        raise HTTPException(status_code=422, detail="limit must be between 1 and 5000")
    if await session.get(GameSession, game_id) is None:
        raise HTTPException(status_code=404, detail="game not found")
    query = select(GameplayEvent).where(GameplayEvent.game_id == game_id)
    if visibility is not None:
        if visibility not in {"policy", "privileged"}:
            raise HTTPException(status_code=422, detail="visibility must be policy or privileged")
        query = query.where(GameplayEvent.visibility == visibility)
    events = list(
        (await session.scalars(query.order_by(GameplayEvent.event_sequence).limit(limit))).all()
    )
    response.headers["X-Result-Limit"] = str(limit)
    return {
        "events": [
            {
                "event_id": event.id,
                "chunk_id": event.chunk_id,
                "game_id": event.game_id,
                "user_id": event.user_id,
                "event_sequence": event.event_sequence,
                "event_type": event.event_type,
                "visibility": event.visibility,
                "client_time_ms": event.client_time_ms,
                "monotonic_time_us": event.monotonic_time_us,
                "simulation_tick": event.simulation_tick,
                "payload": event.payload,
            }
            for event in events
        ]
    }
