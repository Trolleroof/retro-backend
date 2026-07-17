"""Durable PostgreSQL-backed gameplay projection worker."""

from __future__ import annotations

import asyncio
import datetime as dt
import logging

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from .config import get_settings
from .database import create_database_engine, create_session_factory
from .models import GameplayEvent, GameSession, IngestedChunk, ProcessingJob

logger = logging.getLogger(__name__)


async def process_one(factory: async_sessionmaker[AsyncSession]) -> bool:
    async with factory() as session, session.begin():
        now = dt.datetime.now(dt.UTC)
        job = await session.scalar(
            select(ProcessingJob)
            .where(
                ProcessingJob.status.in_(["pending", "retry"]),
                ProcessingJob.available_at <= now,
            )
            .order_by(ProcessingJob.created_at)
            .with_for_update(skip_locked=True)
            .limit(1)
        )
        if job is None:
            return False
        job.status = "processing"
        job.attempts += 1
        job.lease_until = now + dt.timedelta(seconds=get_settings().worker_lease_seconds)
        await session.flush()

        chunk = await session.get(IngestedChunk, job.chunk_id)
        if chunk is None:
            job.status = "failed"
            job.last_error = "chunk no longer exists"
            return True
        game = await session.get(GameSession, chunk.game_id)
        if game is None:
            job.status = "failed"
            job.last_error = "game no longer exists"
            return True

        first, last = (
            await session.execute(
                select(
                    func.min(GameplayEvent.event_sequence),
                    func.max(GameplayEvent.event_sequence),
                ).where(GameplayEvent.game_id == chunk.game_id)
            )
        ).one()
        game.first_event_sequence = first
        game.last_event_sequence = last

        ended = await session.scalar(
            select(GameplayEvent)
            .where(
                GameplayEvent.game_id == chunk.game_id,
                GameplayEvent.event_type.in_(["episode_ended", "game_ended"]),
            )
            .order_by(GameplayEvent.event_sequence.desc())
            .limit(1)
        )
        if ended is not None:
            game.completed = bool(ended.payload.get("completed", True))
            game.final_score = ended.payload.get("final_score")
            game.status = "completed" if game.completed else "abandoned"
        chunk.processing_status = "processed"
        job.status = "completed"
        job.lease_until = None
        job.last_error = None
    return True


async def run() -> None:
    settings = get_settings()
    engine = create_database_engine(settings)
    factory = create_session_factory(engine)
    try:
        while True:
            if not await process_one(factory):
                await asyncio.sleep(settings.worker_poll_seconds)
    finally:
        await engine.dispose()


if __name__ == "__main__":
    logging.basicConfig(level=get_settings().log_level)
    asyncio.run(run())
