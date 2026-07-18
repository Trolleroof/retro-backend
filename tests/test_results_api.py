from collections.abc import AsyncIterator
from types import SimpleNamespace
from typing import Any

from httpx import AsyncClient

MAX_RESULT_BYTES = 16_384


class UnreadBody:
    def __aiter__(self) -> AsyncIterator[bytes]:
        return self

    async def __anext__(self) -> bytes:
        raise AssertionError("unauthenticated request body was read")


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


async def test_result_route_authenticates_before_reading_body(
    app_client: AsyncClient,
) -> None:
    response = await app_client.post(
        "/v1/game-results",
        content=UnreadBody(),
    )

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
    assert not any("high_scores" in sql.casefold() for sql in fake_d1.prepared_sql)


async def test_result_route_rejects_invalid_json_without_score_mutation(
    app_client: AsyncClient,
    fake_d1: Any,
) -> None:
    fake_d1.first_rows.append({"id": "usr_example"})

    response = await app_client.post(
        "/v1/game-results",
        headers={"Authorization": "Bearer opaque"},
        content=b"{not-json",
    )

    assert response.status_code == 422
    assert not any("high_scores" in sql.casefold() for sql in fake_d1.prepared_sql)


async def test_result_route_rejects_oversized_content_length_at_hard_cap(
    app_client: AsyncClient,
    fake_d1: Any,
    fake_env: SimpleNamespace,
) -> None:
    fake_env.MAX_RESULT_BYTES = "1000000"
    fake_d1.first_rows.append({"id": "usr_example"})

    response = await app_client.post(
        "/v1/game-results",
        headers={"Authorization": "Bearer opaque"},
        content=b"x" * (MAX_RESULT_BYTES + 1),
    )

    assert response.status_code == 413
    assert response.json() == {"detail": "request body exceeds maximum size"}
    assert not any("high_scores" in sql.casefold() for sql in fake_d1.prepared_sql)


async def test_result_route_rejects_streamed_overflow_without_score_mutation(
    app_client: AsyncClient,
    fake_d1: Any,
) -> None:
    fake_d1.first_rows.append({"id": "usr_example"})

    async def oversized_body() -> AsyncIterator[bytes]:
        yield b"x" * MAX_RESULT_BYTES
        yield b"x"

    response = await app_client.post(
        "/v1/game-results",
        headers={"Authorization": "Bearer opaque"},
        content=oversized_body(),
    )

    assert response.status_code == 413
    assert response.json() == {"detail": "request body exceeds maximum size"}
    assert not any("high_scores" in sql.casefold() for sql in fake_d1.prepared_sql)


async def test_result_route_requires_auth_and_returns_personal_best(
    app_client: AsyncClient,
    fake_d1: Any,
) -> None:
    fake_d1.first_rows.extend([{"id": "usr_example"}, {"score": "32768"}])

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


async def test_result_route_normalizes_completion_offsets_for_text_ordering(
    app_client: AsyncClient,
    fake_d1: Any,
) -> None:
    fake_d1.first_rows.extend(
        [
            {"id": "usr_example"},
            {"score": "1"},
            {"id": "usr_example"},
            {"score": "1"},
            {"id": "usr_example"},
            {"score": "1"},
        ]
    )
    completed_times = [
        "2026-07-17T20:42:00+02:00",
        "2026-07-17T13:42:00-05:00",
        "2026-07-17T13:41:59.999999-05:00",
    ]

    for completed_at in completed_times:
        payload = _result_payload(score=1)
        payload["completed_at"] = completed_at
        response = await app_client.post(
            "/v1/game-results",
            headers={"Authorization": "Bearer opaque"},
            json=payload,
        )
        assert response.status_code == 202

    bound_times = [
        values[3] for values in fake_d1.bind_calls if len(values) == 4
    ]
    assert bound_times == [
        "2026-07-17T18:42:00.000000Z",
        "2026-07-17T18:42:00.000000Z",
        "2026-07-17T18:41:59.999999Z",
    ]
    assert bound_times[2] < bound_times[0]


async def test_leaderboard_preserves_database_order_and_binds_limit(
    app_client: AsyncClient,
    fake_d1: Any,
) -> None:
    fake_d1.first_rows.append({"id": "usr_example"})
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
