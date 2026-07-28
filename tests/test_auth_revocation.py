"""Multi-device session storage and the bearer-token revocation path."""

import hashlib
import re
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
from httpx import AsyncClient

from retro_backend.auth_cache import (
    DEFAULT_AUTH_CACHE_TTL_SECONDS,
    MAX_AUTH_CACHE_TTL_SECONDS,
)
from retro_backend.storage.d1 import REVOCATION_RETENTION_MODIFIER, D1Repository

ROOT = Path(__file__).resolve().parents[1]
MIGRATION = ROOT / "migrations/0002_auth_sessions.sql"

TOKEN_A = "device-a-token"
TOKEN_B = "device-b-token"
DIGEST_A = hashlib.sha256(TOKEN_A.encode("utf-8")).hexdigest()
DIGEST_B = hashlib.sha256(TOKEN_B.encode("utf-8")).hexdigest()
USER_ID = "usr_multidevice"


def _deleted(*token_hashes: str) -> list[dict[str, str]]:
    return [{"token_hash": token_hash} for token_hash in token_hashes]


def test_migration_creates_session_and_revocation_tables_and_backfills() -> None:
    sql = MIGRATION.read_text(encoding="utf-8")

    assert "CREATE TABLE auth_sessions" in sql
    assert "token_hash TEXT PRIMARY KEY" in sql
    assert "REFERENCES users(id) ON DELETE CASCADE" in sql
    assert "CREATE TABLE revoked_tokens" in sql
    assert re.search(
        r"INSERT INTO auth_sessions .*SELECT auth_token_hash, id, joined_at FROM users",
        sql,
        re.DOTALL,
    )
    # A destructive rebuild of `users` would cascade high_scores away.
    assert "DROP TABLE" not in sql


async def test_second_device_session_does_not_evict_the_first(fake_d1: Any) -> None:
    repository = D1Repository(fake_d1)

    await repository.upsert_auth_session(USER_ID, DIGEST_A)
    await repository.upsert_auth_session(USER_ID, DIGEST_B)

    session_inserts = [
        bind
        for sql, bind in zip(fake_d1.run_calls, fake_d1.bind_calls, strict=True)
        if "INSERT INTO auth_sessions" in sql
    ]
    assert session_inserts == [(DIGEST_A, USER_ID), (DIGEST_B, USER_ID)]
    assert all("DELETE" not in sql for sql in fake_d1.run_calls)


async def test_revoke_session_tombstones_before_deleting(fake_d1: Any) -> None:
    fake_d1.run_rows = [[], _deleted(DIGEST_A), []]

    revoked = await D1Repository(fake_d1).revoke_session(DIGEST_A)

    assert revoked is True
    assert "INSERT INTO revoked_tokens" in fake_d1.run_calls[0]
    assert "DELETE FROM auth_sessions WHERE token_hash = ?1" in fake_d1.run_calls[1]
    assert fake_d1.bind_calls[:2] == [(DIGEST_A,), (DIGEST_A,)]


async def test_revoke_session_reports_no_live_session(fake_d1: Any) -> None:
    fake_d1.run_rows = [[], [], []]

    assert await D1Repository(fake_d1).revoke_session(DIGEST_A) is False


async def test_revoke_user_sessions_counts_every_device(fake_d1: Any) -> None:
    fake_d1.run_rows = [[], _deleted(DIGEST_A, DIGEST_B), []]

    revoked = await D1Repository(fake_d1).revoke_user_sessions(USER_ID)

    assert revoked == 2
    assert "INSERT INTO revoked_tokens" in fake_d1.run_calls[0]
    assert "SELECT token_hash FROM auth_sessions WHERE user_id = ?1" in (
        fake_d1.run_calls[0]
    )
    assert "DELETE FROM auth_sessions WHERE user_id = ?1" in fake_d1.run_calls[1]
    assert fake_d1.bind_calls[:2] == [(USER_ID,), (USER_ID,)]


async def test_revocation_prunes_expired_tombstones(fake_d1: Any) -> None:
    fake_d1.run_rows = [[], _deleted(DIGEST_A), []]

    _ = await D1Repository(fake_d1).revoke_session(DIGEST_A)

    assert "DELETE FROM revoked_tokens WHERE revoked_at < datetime('now', ?1)" in (
        fake_d1.run_calls[-1]
    )
    assert fake_d1.bind_calls[-1] == (REVOCATION_RETENTION_MODIFIER,)


async def test_is_token_revoked_reads_the_tombstone_table(fake_d1: Any) -> None:
    repository = D1Repository(fake_d1)
    fake_d1.first_rows.append({"revoked": 1})

    assert await repository.is_token_revoked(DIGEST_A) is True
    assert "FROM revoked_tokens WHERE token_hash = ?1" in fake_d1.first_calls[-1]
    assert await repository.is_token_revoked(DIGEST_A) is False


async def test_logout_revokes_the_presented_token_and_purges_the_local_cache(
    app_client: AsyncClient,
    fake_d1: Any,
    fake_env: SimpleNamespace,
) -> None:
    fake_d1.first_rows.append({"id": USER_ID})
    fake_d1.run_rows = [[], _deleted(DIGEST_A), []]
    fake_env.AUTH_CACHE.values[DIGEST_A] = USER_ID

    response = await app_client.post(
        "/v1/auth/logout",
        headers={"Authorization": f"Bearer {TOKEN_A}"},
    )

    assert response.status_code == 200
    assert response.json() == {
        "revoked_sessions": 1,
        "scope": "session",
        "edge_cache_ttl_seconds": DEFAULT_AUTH_CACHE_TTL_SECONDS,
    }
    assert fake_env.AUTH_CACHE.delete_calls == [
        ("http://test/v1/auth/logout", DIGEST_A)
    ]
    assert DIGEST_A not in fake_env.AUTH_CACHE.values


async def test_logout_all_devices_revokes_every_session(
    app_client: AsyncClient,
    fake_d1: Any,
) -> None:
    fake_d1.first_rows.append({"id": USER_ID})
    fake_d1.run_rows = [[], _deleted(DIGEST_A, DIGEST_B), []]

    response = await app_client.post(
        "/v1/auth/logout?scope=all_devices",
        headers={"Authorization": f"Bearer {TOKEN_A}"},
    )

    assert response.status_code == 200
    assert response.json()["revoked_sessions"] == 2
    assert response.json()["scope"] == "all_devices"
    assert fake_d1.bind_calls[1:3] == [(USER_ID,), (USER_ID,)]


async def test_logout_reports_the_configured_edge_cache_window(
    app_client: AsyncClient,
    fake_d1: Any,
    fake_env: SimpleNamespace,
) -> None:
    fake_env.AUTH_CACHE_TTL_SECONDS = "86400"
    fake_d1.first_rows.append({"id": USER_ID})
    fake_d1.run_rows = [[], _deleted(DIGEST_A), []]

    response = await app_client.post(
        "/v1/auth/logout",
        headers={"Authorization": f"Bearer {TOKEN_A}"},
    )

    assert response.json()["edge_cache_ttl_seconds"] == MAX_AUTH_CACHE_TTL_SECONDS


async def test_logout_rejects_an_unknown_scope(
    app_client: AsyncClient,
    fake_d1: Any,
) -> None:
    fake_d1.first_rows.append({"id": USER_ID})

    response = await app_client.post(
        "/v1/auth/logout?scope=everything",
        headers={"Authorization": f"Bearer {TOKEN_A}"},
    )

    assert response.status_code == 422
    assert all("DELETE" not in sql for sql in fake_d1.run_calls)


async def test_logout_requires_a_bearer_token(app_client: AsyncClient) -> None:
    response = await app_client.post("/v1/auth/logout")

    assert response.status_code == 401
    assert response.headers["WWW-Authenticate"] == "Bearer"


async def test_logout_survives_an_edge_cache_purge_failure(
    app_client: AsyncClient,
    fake_d1: Any,
    fake_env: SimpleNamespace,
) -> None:
    fake_d1.first_rows.append({"id": USER_ID})
    fake_d1.run_rows = [[], _deleted(DIGEST_A), []]
    fake_env.AUTH_CACHE.delete_failure = RuntimeError("cache unavailable")

    response = await app_client.post(
        "/v1/auth/logout",
        headers={"Authorization": f"Bearer {TOKEN_A}"},
    )

    assert response.status_code == 200
    assert response.json()["revoked_sessions"] == 1


async def test_revoked_token_is_not_remirrored_from_the_auth_service(
    app_client: AsyncClient,
    fake_d1: Any,
    fake_env: SimpleNamespace,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fake_env.AUTH_SESSION_URL = "https://auth.example.com/api/auth/get-session"
    session_calls: list[str] = []

    async def resolve_session(token: str, _session_url: str) -> str:
        session_calls.append(token)
        return USER_ID

    monkeypatch.setattr("retro_backend.auth.resolve_auth_session", resolve_session)
    # No auth_sessions row, but a revoked_tokens tombstone.
    fake_d1.first_rows = [None, {"revoked": 1}]

    response = await app_client.get(
        "/v1/me",
        headers={"Authorization": f"Bearer {TOKEN_A}"},
    )

    assert response.status_code == 401
    assert session_calls == []
    assert all("INSERT INTO auth_sessions" not in sql for sql in fake_d1.run_calls)


async def test_unrevoked_token_still_self_heals_from_the_auth_service(
    app_client: AsyncClient,
    fake_d1: Any,
    fake_env: SimpleNamespace,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fake_env.AUTH_SESSION_URL = "https://auth.example.com/api/auth/get-session"

    async def resolve_session(_token: str, _session_url: str) -> str:
        return USER_ID

    monkeypatch.setattr("retro_backend.auth.resolve_auth_session", resolve_session)
    fake_d1.first_rows = [None, None]

    response = await app_client.get(
        "/v1/me",
        headers={"Authorization": f"Bearer {TOKEN_A}"},
    )

    assert response.status_code == 200
    assert response.json() == {"user_id": USER_ID}
    assert any("INSERT INTO auth_sessions" in sql for sql in fake_d1.run_calls)
