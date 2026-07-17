from types import SimpleNamespace
from typing import Any

from httpx import AsyncClient


def _result_payload(*, game_type: str = "2048", score: int = 32768) -> dict[str, object]:
    return {
        "result_id": "res_01",
        "game_id": "game_01",
        "game_type": game_type,
        "score": score,
        "completed_at": "2026-07-17T18:42:00Z",
    }


async def test_health_live(app_client: AsyncClient) -> None:
    response = await app_client.get("/health/live")

    assert response.status_code == 200
    assert response.json() == {"status": "healthy"}


async def test_health_ready_when_database_binding_is_available(
    app_client: AsyncClient,
) -> None:
    response = await app_client.get("/health/ready")

    assert response.status_code == 200
    assert response.json() == {"status": "ready"}


async def test_health_ready_rejects_missing_database_binding(
    app_client: AsyncClient,
    fake_env: SimpleNamespace,
) -> None:
    database = fake_env.DB
    del fake_env.DB
    try:
        response = await app_client.get("/health/ready")
    finally:
        fake_env.DB = database

    assert response.status_code == 503


async def test_current_user_returns_authenticated_user_id(
    app_client: AsyncClient,
    fake_d1: Any,
) -> None:
    fake_d1.first_rows.append({"id": "usr_example"})

    response = await app_client.get(
        "/v1/me",
        headers={"Authorization": "Bearer opaque"},
    )

    assert response.status_code == 200
    assert response.json() == {"user_id": "usr_example"}


async def test_result_route_requires_bearer_token(app_client: AsyncClient) -> None:
    response = await app_client.post("/v1/game-results", json=_result_payload())

    assert response.status_code == 401


async def test_result_route_rejects_unknown_bearer_token(
    app_client: AsyncClient,
) -> None:
    response = await app_client.post(
        "/v1/game-results",
        headers={"Authorization": "Bearer unknown"},
        json=_result_payload(),
    )

    assert response.status_code == 401


async def test_result_route_rejects_invalid_game_type(
    app_client: AsyncClient,
    fake_d1: Any,
) -> None:
    fake_d1.first_rows.append({"id": "usr_example"})

    response = await app_client.post(
        "/v1/game-results",
        headers={"Authorization": "Bearer opaque"},
        json=_result_payload(game_type="pong"),
    )

    assert response.status_code == 422


async def test_result_route_requires_auth_and_returns_personal_best(
    app_client: AsyncClient,
    fake_d1: Any,
) -> None:
    fake_d1.first_rows.extend([{"id": "usr_example"}, {"score": 32768}])

    response = await app_client.post(
        "/v1/game-results",
        headers={"Authorization": "Bearer opaque"},
        json=_result_payload(),
    )

    assert response.status_code == 202
    assert response.json() == {
        "accepted": True,
        "personal_best": 32768,
        "high_score_updated": True,
    }


async def test_leaderboard_preserves_database_order_and_binds_limit(
    app_client: AsyncClient,
    fake_d1: Any,
) -> None:
    fake_d1.first_rows.append({"id": "usr_example"})
    fake_d1.run_rows.append(
        [
            {
                "user_id": "usr_first",
                "score": 32768,
                "achieved_at": "2026-07-17T18:42:00Z",
            },
            {
                "user_id": "usr_second",
                "score": 16384,
                "achieved_at": "2026-07-17T18:43:00Z",
            },
        ]
    )

    response = await app_client.get(
        "/v1/high-scores/2048?limit=2",
        headers={"Authorization": "Bearer opaque"},
    )

    assert response.status_code == 200
    assert response.json() == {
        "game_type": "2048",
        "high_scores": [
            {
                "user_id": "usr_first",
                "score": 32768,
                "achieved_at": "2026-07-17T18:42:00Z",
            },
            {
                "user_id": "usr_second",
                "score": 16384,
                "achieved_at": "2026-07-17T18:43:00Z",
            },
        ],
    }
    assert fake_d1.bind_calls[-1] == ("2048", 2)
