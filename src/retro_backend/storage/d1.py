"""Prepared-statement repository for D1 product state."""

import hashlib
from dataclasses import dataclass

from ..bindings import D1Database, value_field

_RESOLVE_USER_SQL = "SELECT id FROM users WHERE auth_token_hash = ?1"

_UPSERT_HIGH_SCORE_SQL = """
INSERT INTO high_scores (user_id, game_type, score, achieved_at)
VALUES (?1, ?2, ?3, ?4)
ON CONFLICT(user_id, game_type) DO UPDATE SET
    score = excluded.score,
    achieved_at = excluded.achieved_at
WHERE excluded.score > high_scores.score
RETURNING score
""".strip()

_CURRENT_HIGH_SCORE_SQL = """
SELECT score
FROM high_scores
WHERE user_id = ?1 AND game_type = ?2
""".strip()

_LEADERBOARD_SQL = """
SELECT user_id, score, achieved_at
FROM high_scores
WHERE game_type = ?1
ORDER BY score DESC, achieved_at ASC, user_id ASC
LIMIT ?2
""".strip()


@dataclass(frozen=True, slots=True)
class ScoreUpsertResult:
    personal_best: int
    updated: bool


@dataclass(frozen=True, slots=True)
class LeaderboardEntry:
    user_id: str
    score: int
    achieved_at: str


def _score_from_row(row: object) -> int:
    score = value_field(row, "score")
    if isinstance(score, bool) or not isinstance(score, int):
        raise RuntimeError("D1 high-score row did not contain an integer score")
    return score


class D1Repository:
    def __init__(self, database: D1Database) -> None:
        self.database = database

    async def resolve_user(self, token: str) -> str | None:
        digest = hashlib.sha256(token.encode("utf-8")).hexdigest()
        row = await self.database.prepare(_RESOLVE_USER_SQL).bind(digest).first()
        if row is None:
            return None
        user_id = value_field(row, "id")
        return user_id if isinstance(user_id, str) else None

    async def upsert_high_score(
        self,
        *,
        user_id: str,
        game_type: str,
        score: int,
        achieved_at: str,
    ) -> ScoreUpsertResult:
        row = await (
            self.database.prepare(_UPSERT_HIGH_SCORE_SQL)
            .bind(user_id, game_type, score, achieved_at)
            .first()
        )
        if row is not None:
            return ScoreUpsertResult(personal_best=_score_from_row(row), updated=True)

        current = await (
            self.database.prepare(_CURRENT_HIGH_SCORE_SQL)
            .bind(user_id, game_type)
            .first()
        )
        if current is None:
            raise RuntimeError("D1 high score missing after conditional upsert")
        return ScoreUpsertResult(personal_best=_score_from_row(current), updated=False)

    async def leaderboard(self, game_type: str, *, limit: int) -> list[LeaderboardEntry]:
        result = await self.database.prepare(_LEADERBOARD_SQL).bind(game_type, limit).run()
        rows = value_field(result, "results", [])
        if not isinstance(rows, (list, tuple)):
            return []

        entries: list[LeaderboardEntry] = []
        for row in rows:
            user_id = value_field(row, "user_id")
            achieved_at = value_field(row, "achieved_at")
            if not isinstance(user_id, str) or not isinstance(achieved_at, str):
                raise RuntimeError("D1 leaderboard row did not contain required fields")
            entries.append(
                LeaderboardEntry(
                    user_id=user_id,
                    score=_score_from_row(row),
                    achieved_at=achieved_at,
                )
            )
        return entries
