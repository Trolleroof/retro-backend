import datetime as dt
import hashlib

import pytest
from httpx import AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from retro_backend.models import HighScore, User


def result_payload(*, score: int, completed_at: str) -> dict[str, object]:
    return {
        "result_id": "result_001",
        "game_id": "game_001",
        "game_type": "2048",
        "score": score,
        "completed_at": completed_at,
    }


def as_utc(value: dt.datetime) -> dt.datetime:
    return value if value.tzinfo is not None else value.replace(tzinfo=dt.UTC)


async def test_game_result_creates_personal_best_for_authenticated_user(
    client: AsyncClient,
    auth_headers: dict[str, str],
    fixture_payload: dict[str, object],
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    completed_at = "2026-07-17T12:00:00Z"
    response = await client.post(
        "/v1/game-results",
        json=result_payload(score=1024, completed_at=completed_at),
        headers=auth_headers,
    )

    assert response.status_code == 200
    assert response.json() == {
        "accepted": True,
        "personal_best": 1024,
        "high_score_updated": True,
    }
    async with session_factory() as session:
        score = await session.get(HighScore, (fixture_payload["user_id"], "2048"))
        assert score is not None
        assert score.score == 1024
        assert as_utc(score.achieved_at) == dt.datetime(2026, 7, 17, 12, tzinfo=dt.UTC)


async def test_lower_and_equal_retries_preserve_score_and_achieved_at(
    client: AsyncClient,
    auth_headers: dict[str, str],
    fixture_payload: dict[str, object],
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    original_at = "2026-07-17T12:00:00Z"
    await client.post(
        "/v1/game-results",
        json=result_payload(score=1024, completed_at=original_at),
        headers=auth_headers,
    )
    lower = await client.post(
        "/v1/game-results",
        json=result_payload(score=512, completed_at="2026-07-17T13:00:00Z"),
        headers=auth_headers,
    )
    equal = await client.post(
        "/v1/game-results",
        json=result_payload(score=1024, completed_at="2026-07-17T14:00:00Z"),
        headers=auth_headers,
    )

    assert lower.json() == equal.json() == {
        "accepted": True,
        "personal_best": 1024,
        "high_score_updated": False,
    }
    async with session_factory() as session:
        score = await session.get(HighScore, (fixture_payload["user_id"], "2048"))
        assert score is not None
        assert as_utc(score.achieved_at) == dt.datetime(2026, 7, 17, 12, tzinfo=dt.UTC)


async def test_higher_result_replaces_personal_best_and_timestamp(
    client: AsyncClient,
    auth_headers: dict[str, str],
) -> None:
    await client.post(
        "/v1/game-results",
        json=result_payload(score=100, completed_at="2026-07-17T12:00:00Z"),
        headers=auth_headers,
    )
    response = await client.post(
        "/v1/game-results",
        json=result_payload(score=200, completed_at="2026-07-17T13:00:00Z"),
        headers=auth_headers,
    )
    assert response.json() == {
        "accepted": True,
        "personal_best": 200,
        "high_score_updated": True,
    }


async def test_high_scores_are_ranked_and_limited(
    client: AsyncClient,
    auth_headers: dict[str, str],
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    async with session_factory() as session:
        for user_id in ("usr_early", "usr_late", "usr_low"):
            session.add(User(id=user_id, auth_token_hash=hashlib.sha256(user_id.encode()).hexdigest()))
        await session.flush()
        session.add_all(
            [
                HighScore(
                    user_id="usr_late",
                    game_type="2048",
                    score=200,
                    achieved_at=dt.datetime(2026, 7, 17, 13, tzinfo=dt.UTC),
                ),
                HighScore(
                    user_id="usr_early",
                    game_type="2048",
                    score=200,
                    achieved_at=dt.datetime(2026, 7, 17, 12, tzinfo=dt.UTC),
                ),
                HighScore(
                    user_id="usr_low",
                    game_type="2048",
                    score=100,
                    achieved_at=dt.datetime(2026, 7, 17, 11, tzinfo=dt.UTC),
                ),
            ]
        )
        await session.commit()

    response = await client.get("/v1/high-scores/2048?limit=2", headers=auth_headers)
    assert response.status_code == 200
    assert [entry["user_id"] for entry in response.json()["high_scores"]] == [
        "usr_early",
        "usr_late",
    ]


@pytest.mark.parametrize(
    "game_type",
    ["2048", "tetris", "snake", "minesweeper", "sokoban", "connect4", "lightsout", "sudoku"],
)
async def test_all_supported_games_accept_results(
    client: AsyncClient, auth_headers: dict[str, str], game_type: str
) -> None:
    payload = result_payload(score=1, completed_at="2026-07-17T12:00:00Z")
    payload["game_type"] = game_type
    response = await client.post("/v1/game-results", json=payload, headers=auth_headers)
    assert response.status_code == 200


async def test_results_reject_invalid_game_and_negative_score(
    client: AsyncClient, auth_headers: dict[str, str]
) -> None:
    invalid_game = result_payload(score=1, completed_at="2026-07-17T12:00:00Z")
    invalid_game["game_type"] = "pong"
    bad_game = await client.post("/v1/game-results", json=invalid_game, headers=auth_headers)
    bad_score = await client.post(
        "/v1/game-results",
        json=result_payload(score=-1, completed_at="2026-07-17T12:00:00Z"),
        headers=auth_headers,
    )
    boolean_score = result_payload(score=1, completed_at="2026-07-17T12:00:00Z")
    boolean_score["score"] = True
    bad_boolean = await client.post(
        "/v1/game-results", json=boolean_score, headers=auth_headers
    )
    bad_limit = await client.get("/v1/high-scores/2048?limit=101", headers=auth_headers)

    assert bad_game.status_code == 422
    assert bad_score.status_code == 422
    assert bad_boolean.status_code == 422
    assert bad_limit.status_code == 422
