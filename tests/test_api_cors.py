from types import SimpleNamespace

import pytest
from httpx import AsyncClient

pytestmark = pytest.mark.usefixtures("app_client")


async def test_me_options_preflight_allows_configured_origin(
    app_client: AsyncClient,
    fake_env: SimpleNamespace,
) -> None:
    fake_env.ALLOWED_GAME_ORIGINS = "https://game.example"

    response = await app_client.options(
        "/v1/me", headers={"Origin": "https://game.example"}
    )

    assert response.status_code == 204
    assert response.headers["access-control-allow-origin"] == "https://game.example"
    assert response.headers["access-control-allow-methods"] == "GET"
    assert response.headers["vary"] == "Origin"


async def test_me_options_preflight_rejects_unconfigured_origin(
    app_client: AsyncClient,
    fake_env: SimpleNamespace,
) -> None:
    fake_env.ALLOWED_GAME_ORIGINS = "https://game.example"

    response = await app_client.options(
        "/v1/me", headers={"Origin": "https://evil.example"}
    )

    assert response.status_code == 403
    assert "access-control-allow-origin" not in response.headers


async def test_me_request_from_allowed_origin_gets_cors_header(
    app_client: AsyncClient,
    fake_env: SimpleNamespace,
) -> None:
    fake_env.ALLOWED_GAME_ORIGINS = "https://game.example"

    response = await app_client.get(
        "/v1/me", headers={"Origin": "https://game.example"}
    )

    assert response.status_code == 401
    assert response.headers["access-control-allow-origin"] == "https://game.example"


async def test_me_request_from_disallowed_origin_is_rejected_before_route(
    app_client: AsyncClient,
    fake_env: SimpleNamespace,
) -> None:
    fake_env.ALLOWED_GAME_ORIGINS = "https://game.example"

    response = await app_client.get(
        "/v1/me",
        headers={
            "Origin": "https://evil.example",
            "Authorization": "Bearer opaque",
        },
    )

    assert response.status_code == 403
    assert "access-control-allow-origin" not in response.headers


async def test_me_request_without_origin_is_unaffected(
    app_client: AsyncClient,
) -> None:
    response = await app_client.get("/v1/me")

    assert response.status_code == 401
    assert "access-control-allow-origin" not in response.headers


async def test_high_scores_options_preflight_uses_get(
    app_client: AsyncClient,
    fake_env: SimpleNamespace,
) -> None:
    fake_env.ALLOWED_GAME_ORIGINS = "https://game.example"

    response = await app_client.options(
        "/v1/high-scores/2048", headers={"Origin": "https://game.example"}
    )

    assert response.status_code == 204
    assert response.headers["access-control-allow-methods"] == "GET"


async def test_game_results_options_preflight_uses_post(
    app_client: AsyncClient,
    fake_env: SimpleNamespace,
) -> None:
    fake_env.ALLOWED_GAME_ORIGINS = "https://game.example"

    response = await app_client.options(
        "/v1/game-results", headers={"Origin": "https://game.example"}
    )

    assert response.status_code == 204
    assert response.headers["access-control-allow-methods"] == "POST"


async def test_game_event_chunks_options_preflight_uses_post(
    app_client: AsyncClient,
    fake_env: SimpleNamespace,
) -> None:
    fake_env.ALLOWED_GAME_ORIGINS = "https://game.example"

    response = await app_client.options(
        "/v1/game-event-chunks", headers={"Origin": "https://game.example"}
    )

    assert response.status_code == 204
    assert response.headers["access-control-allow-methods"] == "POST"


async def test_room_routes_are_unaffected_by_api_cors_middleware(
    app_client: AsyncClient,
) -> None:
    response = await app_client.options(
        "/health/live", headers={"Origin": "https://evil.example"}
    )

    assert response.status_code == 405
