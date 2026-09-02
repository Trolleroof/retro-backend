"""Role permission matrix and per-tool argument schemas (#57)."""

import pytest
from pydantic import ValidationError

from retro_backend.esim.tools import (
    ROLE_TOOLS,
    TOOL_ARGS,
    ServeArgs,
    role_permits,
)


def test_every_role_can_send_message_and_request_help():
    for role, tools in ROLE_TOOLS.items():
        assert "send_message" in tools, role
        assert "request_help" in tools, role


def test_role_tool_matrix_is_partitioned_by_job():
    assert role_permits("chef", "prepare")
    assert not role_permits("chef", "serve")
    assert role_permits("server", "serve")
    assert not role_permits("server", "seat")
    assert role_permits("host", "seat")
    assert not role_permits("host", "prepare")
    assert role_permits("expediter", "coordinate")
    assert not role_permits("expediter", "serve")


def test_every_tool_has_an_argument_schema():
    from typing import get_args

    from retro_backend.esim.contracts import Tool

    assert set(TOOL_ARGS) == set(get_args(Tool))


def test_serve_args_default_unverified():
    args = ServeArgs.model_validate({"orderId": "order-2"})
    assert args.verified is False


def test_tool_args_reject_unknown_fields():
    with pytest.raises(ValidationError):
        ServeArgs.model_validate({"orderId": "order-2", "verified": True, "extra": 1})
