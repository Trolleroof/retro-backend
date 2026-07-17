"""Atomic personal-best updates."""

from __future__ import annotations

import datetime as dt

from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert as postgresql_insert
from sqlalchemy.dialects.sqlite import insert as sqlite_insert
from sqlalchemy.ext.asyncio import AsyncSession

from ..models import HighScore


async def upsert_personal_best(
    session: AsyncSession,
    *,
    user_id: str,
    game_type: str,
    score: int,
    achieved_at: dt.datetime,
) -> tuple[int, bool]:
    values = {
        "user_id": user_id,
        "game_type": game_type,
        "score": score,
        "achieved_at": achieved_at,
    }
    if session.get_bind().dialect.name == "postgresql":
        insert_statement = postgresql_insert(HighScore).values(**values)
    else:
        insert_statement = sqlite_insert(HighScore).values(**values)
    statement = insert_statement.on_conflict_do_update(
        index_elements=[HighScore.user_id, HighScore.game_type],
        set_={
            "score": insert_statement.excluded.score,
            "achieved_at": insert_statement.excluded.achieved_at,
        },
        where=insert_statement.excluded.score > HighScore.score,
    ).returning(HighScore.score)
    updated_score = await session.scalar(statement)
    if updated_score is not None:
        return updated_score, True
    personal_best = await session.scalar(
        select(HighScore.score).where(
            HighScore.user_id == user_id,
            HighScore.game_type == game_type,
        )
    )
    if personal_best is None:
        raise RuntimeError("personal best upsert returned no score")
    return personal_best, False
