"""Pin the identity contract the tui-gamepigeon client must satisfy.

The Worker derives the authenticated user as ``usr_`` + the first 32 hex
characters of ``sha256(auth_user_id)`` and then requires the chunk envelope and
every event to carry exactly that value. The client currently derives recording
identities two other ways, neither of which can ever match. These tests mirror
the client's derivations so that CI fails loudly the moment either side drifts,
instead of the mismatch surfacing as a blanket 400 in production.

Client source of truth: ``src/recording/identity.ts`` and ``src/recording/ids.ts``
in https://github.com/Trolleroof/tui-gamepigeon.
"""

import json
import re
from typing import Any
from uuid import uuid4

import pytest
from httpx import AsyncClient

from retro_backend.auth import canonical_user_id
from retro_backend.contracts import ContractError, parse_chunk_envelope

# A representative Better Auth user id: opaque, alphanumeric, no separators.
BETTER_AUTH_USER_ID = "3xK9mQp2vLn7bT4wR8yZ"


def client_normalize_account_id(value: str) -> str:
    """Mirror ``normalizeAccountId`` from the client's ``recording/identity.ts``."""
    if value.startswith("usr_") and re.fullmatch(r"usr_[A-Za-z0-9._-]+", value):
        return value
    if re.fullmatch(r"[A-Za-z0-9._-]+", value):
        return f"usr_{value}"
    from hashlib import sha256

    return f"usr_{sha256(value.encode('utf-8')).hexdigest()[:32]}"


def client_local_user_id() -> str:
    """Mirror ``createUserId`` — the id written to ``~/.tui-gamepigeon/user_id``."""
    return f"usr_{uuid4()}"


def test_canonical_user_id_is_prefixed_truncated_sha256() -> None:
    from hashlib import sha256

    digest = sha256(BETTER_AUTH_USER_ID.encode("utf-8")).hexdigest()[:32]
    assert canonical_user_id(BETTER_AUTH_USER_ID) == f"usr_{digest}"
    assert len(canonical_user_id(BETTER_AUTH_USER_ID)) == len("usr_") + 32


def test_client_account_derivation_cannot_match_server_canonical_id() -> None:
    """``normalizeAccountId`` prefixes a Better Auth id rather than hashing it."""
    client_value = client_normalize_account_id(BETTER_AUTH_USER_ID)

    assert client_value == f"usr_{BETTER_AUTH_USER_ID}"
    assert client_value != canonical_user_id(BETTER_AUTH_USER_ID)


def test_client_local_identity_cannot_match_server_canonical_id() -> None:
    """The offline ``usr_<uuid>`` fallback is unrelated to the account identity."""
    assert client_local_user_id() != canonical_user_id(BETTER_AUTH_USER_ID)


@pytest.mark.parametrize(
    "derive",
    [
        pytest.param(
            lambda: client_normalize_account_id(BETTER_AUTH_USER_ID),
            id="normalize-account-id",
        ),
        pytest.param(client_local_user_id, id="local-user-id-file"),
    ],
)
def test_client_derived_identities_are_rejected_by_the_chunk_contract(
    fixture_payload: dict[str, Any],
    derive: Any,
) -> None:
    authenticated = canonical_user_id(BETTER_AUTH_USER_ID)
    client_value = derive()
    fixture_payload["user_id"] = client_value
    for event in fixture_payload["events"]:
        event["user_id"] = client_value

    with pytest.raises(ContractError) as error:
        parse_chunk_envelope(json.dumps(fixture_payload).encode(), authenticated)

    # The rejection must name both sides so the client can self-diagnose.
    assert authenticated in str(error.value)
    assert client_value in str(error.value)
    assert "GET /v1/me" in str(error.value)


def test_canonical_identity_satisfies_the_chunk_contract(
    fixture_payload: dict[str, Any],
) -> None:
    authenticated = canonical_user_id(BETTER_AUTH_USER_ID)
    fixture_payload["user_id"] = authenticated
    for event in fixture_payload["events"]:
        event["user_id"] = authenticated

    envelope = parse_chunk_envelope(
        json.dumps(fixture_payload).encode(),
        authenticated,
    )

    assert envelope.user_id == authenticated


async def test_me_returns_the_identity_clients_must_record_with(
    app_client: AsyncClient,
    fake_d1: Any,
) -> None:
    """``GET /v1/me`` is the only supported source of the recording identity."""
    authenticated = canonical_user_id(BETTER_AUTH_USER_ID)
    fake_d1.first_rows.append({"id": authenticated})

    response = await app_client.get(
        "/v1/me",
        headers={"Authorization": "Bearer opaque"},
    )

    assert response.status_code == 200
    assert response.json() == {"user_id": authenticated}
