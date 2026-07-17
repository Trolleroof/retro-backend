from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from retro_backend.models import GameSession, IngestedChunk, ProcessingJob
from retro_backend.services.ingestion import ingest_chunk
from retro_backend.worker import process_one


async def test_worker_projects_episode_completion_and_completes_job(
    fixture_bytes: bytes,
    fixture_payload: dict[str, Any],
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    async with session_factory() as session:
        await ingest_chunk(session, fixture_bytes)
        await session.commit()

    assert await process_one(session_factory) is True

    async with session_factory() as session:
        chunk = await session.get(IngestedChunk, fixture_payload["chunk_id"])
        game = await session.get(GameSession, fixture_payload["game_id"])
        job = await session.get(ProcessingJob, f"process:{fixture_payload['chunk_id']}")
        assert chunk is not None and chunk.processing_status == "processed"
        assert job is not None and job.status == "completed"
        assert game is not None
        assert game.first_event_sequence == fixture_payload["first_event_sequence"]
        assert game.last_event_sequence == fixture_payload["last_event_sequence"]
