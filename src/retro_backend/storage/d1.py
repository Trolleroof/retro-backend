"""Prepared-statement repository for D1 product state."""

import hashlib
from dataclasses import dataclass

from ..bindings import D1Database, is_binding_null, iter_binding_values, value_field

_RESOLVE_USER_SQL = "SELECT user_id AS id FROM auth_sessions WHERE token_hash = ?1"

_UPSERT_USER_SQL = """
INSERT INTO users (id, auth_token_hash)
VALUES (?1, ?2)
ON CONFLICT(id) DO UPDATE SET auth_token_hash = excluded.auth_token_hash
""".strip()

_INSERT_AUTH_SESSION_SQL = """
INSERT INTO auth_sessions (token_hash, user_id)
VALUES (?1, ?2)
ON CONFLICT(token_hash) DO UPDATE SET user_id = excluded.user_id
""".strip()

_IS_TOKEN_REVOKED_SQL = "SELECT 1 AS revoked FROM revoked_tokens WHERE token_hash = ?1"

_TOMBSTONE_TOKEN_SQL = """
INSERT INTO revoked_tokens (token_hash)
VALUES (?1)
ON CONFLICT(token_hash) DO NOTHING
""".strip()

_TOMBSTONE_USER_SESSIONS_SQL = """
INSERT INTO revoked_tokens (token_hash)
SELECT token_hash FROM auth_sessions WHERE user_id = ?1
ON CONFLICT(token_hash) DO NOTHING
""".strip()

_DELETE_SESSION_SQL = "DELETE FROM auth_sessions WHERE token_hash = ?1 RETURNING token_hash"

_DELETE_USER_SESSIONS_SQL = "DELETE FROM auth_sessions WHERE user_id = ?1 RETURNING token_hash"

_PRUNE_REVOCATIONS_SQL = "DELETE FROM revoked_tokens WHERE revoked_at < datetime('now', ?1)"

REVOCATION_RETENTION_MODIFIER = "-30 days"

_UPSERT_HIGH_SCORE_SQL = """
INSERT INTO high_scores (user_id, game_type, score, achieved_at)
VALUES (?1, ?2, CAST(?3 AS INTEGER), ?4)
ON CONFLICT(user_id, game_type) DO UPDATE SET
    score = excluded.score,
    achieved_at = excluded.achieved_at
WHERE excluded.score > high_scores.score
RETURNING CAST(score AS TEXT) AS score
""".strip()

_CURRENT_HIGH_SCORE_SQL = """
SELECT CAST(score AS TEXT) AS score
FROM high_scores
WHERE user_id = ?1 AND game_type = ?2
""".strip()

_LEADERBOARD_SQL = """
SELECT user_id, CAST(score AS TEXT) AS score, achieved_at
FROM high_scores
WHERE game_type = ?1
ORDER BY high_scores.score DESC, achieved_at ASC, user_id ASC
LIMIT ?2
""".strip()

_MAX_SCORE = 2**63 - 1


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
    score_text = value_field(row, "score")
    if (
        not isinstance(score_text, str)
        or not score_text.isascii()
        or not score_text.isdecimal()
    ):
        raise RuntimeError("D1 high-score row did not contain a decimal score")
    score = int(score_text)
    if score > _MAX_SCORE:
        raise RuntimeError("D1 high-score row exceeded the signed 64-bit range")
    return score


def _deleted_row_count(result: object) -> int:
    """Count the rows a ``DELETE ... RETURNING token_hash`` statement removed."""
    rows = value_field(result, "results", [])
    return sum(
        1
        for row in iter_binding_values(rows)
        if isinstance(value_field(row, "token_hash"), str)
    )


class D1Repository:
    def __init__(self, database: D1Database) -> None:
        self.database = database

    async def resolve_user(self, token: str) -> str | None:
        digest = hashlib.sha256(token.encode("utf-8")).hexdigest()
        return await self.resolve_user_hash(digest)

    async def resolve_user_hash(self, token_digest: str) -> str | None:
        row = (
            await self.database.prepare(_RESOLVE_USER_SQL)
            .bind(token_digest)
            .first()
        )
        if row is None:
            return None
        user_id = value_field(row, "id")
        return user_id if isinstance(user_id, str) else None

    async def upsert_auth_session(self, user_id: str, token_digest: str) -> None:
        """Mirror one device token, leaving this user's other devices signed in."""
        await self.database.prepare(_UPSERT_USER_SQL).bind(user_id, token_digest).run()
        await (
            self.database.prepare(_INSERT_AUTH_SESSION_SQL)
            .bind(token_digest, user_id)
            .run()
        )

    async def is_token_revoked(self, token_digest: str) -> bool:
        row = await self.database.prepare(_IS_TOKEN_REVOKED_SQL).bind(token_digest).first()
        return not is_binding_null(row)

    async def revoke_session(self, token_digest: str) -> bool:
        """Revoke one device token, returning whether a live session was removed."""
        # Tombstone before deleting: a concurrent request that misses the session
        # row must find the tombstone rather than re-mirror the token from the
        # upstream auth service.
        await self.database.prepare(_TOMBSTONE_TOKEN_SQL).bind(token_digest).run()
        result = await self.database.prepare(_DELETE_SESSION_SQL).bind(token_digest).run()
        await self._prune_revocations()
        return _deleted_row_count(result) > 0

    async def revoke_user_sessions(self, user_id: str) -> int:
        """Revoke every device token for one user, returning how many were live."""
        await self.database.prepare(_TOMBSTONE_USER_SESSIONS_SQL).bind(user_id).run()
        result = await self.database.prepare(_DELETE_USER_SESSIONS_SQL).bind(user_id).run()
        await self._prune_revocations()
        return _deleted_row_count(result)

    async def _prune_revocations(self) -> None:
        await (
            self.database.prepare(_PRUNE_REVOCATIONS_SQL)
            .bind(REVOCATION_RETENTION_MODIFIER)
            .run()
        )

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
            .bind(user_id, game_type, str(score), achieved_at)
            .first()
        )
        if not is_binding_null(row):
            return ScoreUpsertResult(personal_best=_score_from_row(row), updated=True)

        current = await (
            self.database.prepare(_CURRENT_HIGH_SCORE_SQL)
            .bind(user_id, game_type)
            .first()
        )
        if is_binding_null(current):
            raise RuntimeError("D1 high score missing after conditional upsert")
        return ScoreUpsertResult(personal_best=_score_from_row(current), updated=False)

    async def leaderboard(self, game_type: str, *, limit: int) -> list[LeaderboardEntry]:
        result = await self.database.prepare(_LEADERBOARD_SQL).bind(game_type, limit).run()
        rows = value_field(result, "results", [])

        entries: list[LeaderboardEntry] = []
        for row in iter_binding_values(rows):
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
