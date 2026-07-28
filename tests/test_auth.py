import hashlib
import inspect
import json
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace
from typing import Any, cast

import pytest
from fastapi import HTTPException

from retro_backend.auth import canonical_user_id, hash_token, parse_bearer_token
from retro_backend.bindings import DurableObjectNamespace, value_field
from retro_backend.observability import ALLOWED_EVENTS, ALLOWED_OUTCOMES, log_event

ROOT = Path(__file__).resolve().parents[1]


def test_value_field_reads_mappings_and_object_attributes() -> None:
    assert value_field({"name": "mapping"}, "name") == "mapping"
    assert value_field(SimpleNamespace(name="attribute"), "name") == "attribute"
    assert value_field({}, "missing", "fallback") == "fallback"


def test_durable_object_namespace_uses_runtime_method_spelling() -> None:
    assert "idFromName" in DurableObjectNamespace.__dict__
    assert "id_from_name" not in DurableObjectNamespace.__dict__


def test_token_hash_is_stable_and_does_not_contain_plaintext() -> None:
    digest = hash_token("retro_secret")

    assert digest == hashlib.sha256(b"retro_secret").hexdigest()
    assert "retro_secret" not in digest


def test_canonical_user_id_is_stable_and_backend_safe() -> None:
    user_id = canonical_user_id("better-auth-user-123")

    assert user_id == canonical_user_id("better-auth-user-123")
    assert user_id.startswith("usr_")
    assert len(user_id) == 36


def test_strict_bearer_parser_accepts_one_opaque_token() -> None:
    assert parse_bearer_token("Bearer opaque-token._~+") == "opaque-token._~+"


@pytest.mark.parametrize(
    "authorization",
    [
        None,
        "",
        "bearer opaque",
        "Bearer",
        "Bearer ",
        "Bearer opaque extra",
        " Bearer opaque",
        "Bearer\topaque",
    ],
)
def test_strict_bearer_parser_rejects_malformed_headers(
    authorization: str | None,
) -> None:
    with pytest.raises(HTTPException) as raised:
        parse_bearer_token(authorization)

    assert raised.value.status_code == 401


def test_log_event_prints_only_supplied_allowlisted_fields(
    capsys: pytest.CaptureFixture[str],
) -> None:
    log_event(
        "score_submitted",
        request_id="req_01",
        user_id="usr_01",
        game_id="game_01",
        chunk_id="chk_01",
        outcome="accepted",
    )

    output = capsys.readouterr().out
    assert output == (
        '{"event":"score_submitted","request_id":"req_01",'
        '"user_id":"usr_01","game_id":"game_01","chunk_id":"chk_01",'
        '"outcome":"accepted"}\n'
    )
    assert json.loads(output) == {
        "event": "score_submitted",
        "request_id": "req_01",
        "user_id": "usr_01",
        "game_id": "game_01",
        "chunk_id": "chk_01",
        "outcome": "accepted",
    }


def test_log_event_exposes_finite_known_choices() -> None:
    assert frozenset({"score_submitted"}) == ALLOWED_EVENTS
    assert frozenset({"accepted"}) == ALLOWED_OUTCOMES


@pytest.mark.parametrize(
    ("event", "outcome"),
    [
        ("unknown_event", "accepted"),
        ("score_submitted", "unknown_outcome"),
        ("Bearer opaque-token", "accepted"),
        ("score_submitted", "t=1720000000,v1=signature"),
    ],
)
def test_log_event_rejects_unknown_event_and_outcome(
    event: str, outcome: str
) -> None:
    with pytest.raises(ValueError):
        log_event(event, outcome=outcome)


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("request_id", "Bearer opaque-token"),
        ("user_id", "t=1720000000,v1=signature"),
        ("game_id", '{"token":"opaque"}'),
        ("chunk_id", "chk_" + "a" * 121),
    ],
)
def test_log_event_rejects_unstructured_or_oversized_identifiers(
    field: str, value: str
) -> None:
    with pytest.raises(ValueError):
        cast(Any, log_event)(
            "score_submitted",
            outcome="accepted",
            **{field: value},
        )


def test_log_event_rejects_non_string_identifiers() -> None:
    with pytest.raises(TypeError):
        cast(Any, log_event)(
            "score_submitted",
            user_id=b"usr_01",
            outcome="accepted",
        )


def test_log_event_api_has_no_raw_or_arbitrary_field_channel() -> None:
    parameters = inspect.signature(log_event).parameters

    assert set(parameters) == {
        "event",
        "request_id",
        "user_id",
        "game_id",
        "chunk_id",
        "outcome",
    }
    assert all(
        parameter.kind is not inspect.Parameter.VAR_KEYWORD
        for parameter in parameters.values()
    )
    with pytest.raises(TypeError, match="event must be a string"):
        cast(Any, log_event)({"token": "must-not-be-logged"})


def test_issue_token_separates_one_time_secret_from_parameter_safe_sql() -> None:
    completed = subprocess.run(
        [sys.executable, str(ROOT / "scripts/issue_token.py")],
        check=False,
        capture_output=True,
        text=True,
    )

    assert completed.returncode == 0
    token_lines = completed.stderr.splitlines()
    assert len(token_lines) == 1
    token = token_lines[0]
    assert token
    digest = hash_token(token)
    assert token not in completed.stdout
    assert completed.stdout == (
        "INSERT INTO users (id, auth_token_hash) "
        f"VALUES (?1, '{digest}');\n"
        "INSERT INTO auth_sessions (token_hash, user_id) "
        f"VALUES ('{digest}', ?1);\n"
    )
