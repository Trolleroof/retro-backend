from httpx import AsyncClient


async def test_me_returns_authenticated_user_id_exactly(
    client: AsyncClient,
    auth_headers: dict[str, str],
    fixture_payload: dict[str, object],
) -> None:
    response = await client.get("/v1/me", headers=auth_headers)

    assert response.status_code == 200
    assert response.json() == {"user_id": fixture_payload["user_id"]}


async def test_me_rejects_missing_bearer_token(client: AsyncClient) -> None:
    response = await client.get("/v1/me")

    assert response.status_code == 401
    assert response.json() == {"detail": "Missing bearer token"}


async def test_me_rejects_invalid_bearer_token(client: AsyncClient) -> None:
    response = await client.get(
        "/v1/me",
        headers={"Authorization": "Bearer retro_not-issued"},
    )

    assert response.status_code == 401
    assert response.json() == {"detail": "Invalid bearer token"}
