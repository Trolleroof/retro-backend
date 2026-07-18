import sqlite3
from collections.abc import Iterator
from pathlib import Path
from typing import Any, override

import pytest

from retro_backend.auth import hash_token
from retro_backend.bindings import value_field
from retro_backend.storage.d1 import D1Repository

ROOT = Path(__file__).resolve().parents[1]


class JsNull:
    typeof = "object"

    @override
    def __repr__(self) -> str:
        return "jsnull"

    def __bool__(self) -> bool:
        return False


class IterableProxy:
    def __init__(self, values: list[object]) -> None:
        self.values = values

    def __iter__(self) -> Iterator[object]:
        return iter(self.values)


def _compact_sql(sql: str) -> str:
    return " ".join(sql.split())


def test_value_field_normalizes_jsnull_to_default() -> None:
    assert value_field({"score": JsNull()}, "score", "missing") == "missing"


async def test_auth_repository_resolves_token_hash(fake_d1: Any) -> None:
    fake_d1.first_rows.append({"id": "usr_example"})

    user_id = await D1Repository(fake_d1).resolve_user("opaque")

    assert user_id == "usr_example"
    assert fake_d1.bind_calls[-1] == (hash_token("opaque"),)
    assert "auth_token_hash = ?1" in fake_d1.prepared_sql[-1]


async def test_auth_repository_accepts_precomputed_token_hash(fake_d1: Any) -> None:
    digest = hash_token("opaque")
    fake_d1.first_rows.append({"id": "usr_example"})

    user_id = await D1Repository(fake_d1).resolve_user_hash(digest)

    assert user_id == "usr_example"
    assert fake_d1.bind_calls == [(digest,)]


async def test_auth_repository_returns_none_for_unknown_token(fake_d1: Any) -> None:
    assert await D1Repository(fake_d1).resolve_user("unknown") is None


async def test_higher_score_updates_personal_best(fake_d1: Any) -> None:
    fake_d1.first_rows.append({"score": "100"})

    result = await D1Repository(fake_d1).upsert_high_score(
        user_id="usr_example",
        game_type="2048",
        score=100,
        achieved_at="2026-07-17T18:42:00Z",
    )

    assert result.personal_best == 100
    assert result.updated is True
    assert fake_d1.bind_calls == (
        [
            (
                "usr_example",
                "2048",
                "100",
                "2026-07-17T18:42:00Z",
            )
        ]
    )
    assert _compact_sql(fake_d1.prepared_sql[0]) == _compact_sql(
        """
        INSERT INTO high_scores (user_id, game_type, score, achieved_at)
        VALUES (?1, ?2, CAST(?3 AS INTEGER), ?4)
        ON CONFLICT(user_id, game_type) DO UPDATE SET
            score = excluded.score,
            achieved_at = excluded.achieved_at
        WHERE excluded.score > high_scores.score
        RETURNING CAST(score AS TEXT) AS score
        """
    )


@pytest.mark.parametrize("score", [2**53 - 1, 2**53, 2**63 - 1])
async def test_score_transport_preserves_signed_64_bit_boundaries(
    fake_d1: Any, score: int
) -> None:
    fake_d1.first_rows.append({"score": str(score)})

    result = await D1Repository(fake_d1).upsert_high_score(
        user_id="usr_example",
        game_type="2048",
        score=score,
        achieved_at="2026-07-17T18:42:00Z",
    )

    assert result.personal_best == score
    assert fake_d1.bind_calls[0][2] == str(score)


@pytest.mark.parametrize("submitted_score", [99, 100])
async def test_lower_or_equal_score_preserves_personal_best(
    fake_d1: Any, submitted_score: int
) -> None:
    fake_d1.first_rows.extend([None, {"score": "100"}])

    result = await D1Repository(fake_d1).upsert_high_score(
        user_id="usr_example",
        game_type="2048",
        score=submitted_score,
        achieved_at="2026-07-17T18:42:00Z",
    )

    assert result.personal_best == 100
    assert result.updated is False
    assert fake_d1.bind_calls[-1] == ("usr_example", "2048")
    assert "WHERE user_id = ?1 AND game_type = ?2" in _compact_sql(
        fake_d1.prepared_sql[-1]
    )


async def test_lower_score_treats_jsnull_returning_row_as_no_row(fake_d1: Any) -> None:
    fake_d1.first_rows.extend([JsNull(), {"score": "100"}])

    result = await D1Repository(fake_d1).upsert_high_score(
        user_id="usr_example",
        game_type="2048",
        score=99,
        achieved_at="2026-07-17T18:42:00Z",
    )

    assert result.personal_best == 100
    assert result.updated is False


async def test_leaderboard_uses_descending_order_and_limit(fake_d1: Any) -> None:
    fake_d1.run_rows.append(
        [
            {
                "user_id": "usr_first",
                "score": "32768",
                "achieved_at": "2026-07-17T18:42:00Z",
            },
            {
                "user_id": "usr_second",
                "score": "16384",
                "achieved_at": "2026-07-17T18:43:00Z",
            },
        ]
    )

    entries = await D1Repository(fake_d1).leaderboard("2048", limit=2)

    assert [entry.user_id for entry in entries] == ["usr_first", "usr_second"]
    assert [entry.score for entry in entries] == [32768, 16384]
    assert fake_d1.bind_calls[-1] == ("2048", 2)
    sql = _compact_sql(fake_d1.prepared_sql[-1])
    assert "CAST(score AS TEXT) AS score" in sql
    assert "ORDER BY high_scores.score DESC, achieved_at ASC, user_id ASC" in sql
    assert sql.endswith("LIMIT ?2")


async def test_leaderboard_accepts_iterable_proxy_results(fake_d1: Any) -> None:
    fake_d1.run_rows.append(
        IterableProxy(
            [
                {
                    "user_id": "usr_first",
                    "score": "32768",
                    "achieved_at": "2026-07-17T18:42:00Z",
                }
            ]
        )
    )

    entries = await D1Repository(fake_d1).leaderboard("2048", limit=1)

    assert len(entries) == 1
    assert entries[0].user_id == "usr_first"
    assert entries[0].score == 32768


async def test_current_score_fallback_selects_score_as_text(fake_d1: Any) -> None:
    fake_d1.first_rows.extend([None, {"score": "100"}])

    await D1Repository(fake_d1).upsert_high_score(
        user_id="usr_example",
        game_type="2048",
        score=99,
        achieved_at="2026-07-17T18:42:00Z",
    )

    assert "SELECT CAST(score AS TEXT) AS score" in _compact_sql(
        fake_d1.prepared_sql[-1]
    )


def test_core_migration_builds_approved_d1_schema_and_leaderboard_index() -> None:
    migration = (ROOT / "migrations/0001_core.sql").read_text()
    connection = sqlite3.connect(":memory:")
    connection.executescript(migration)

    tables = {
        row[0]
        for row in connection.execute(
            "SELECT name FROM sqlite_master WHERE type = 'table'"
        )
    }
    assert tables == {"users", "high_scores"}
    assert not any(
        marker in migration.casefold()
        for marker in ("stripe", "posthog", "billing", "analytics")
    )
    assert connection.execute("PRAGMA foreign_keys").fetchone() == (1,)
    index_sql = connection.execute(
        "SELECT sql FROM sqlite_master WHERE type = 'index' "
        + "AND name = 'high_scores_leaderboard_idx'"
    ).fetchone()
    assert index_sql is not None
    assert "game_type, score DESC" in index_sql[0]
