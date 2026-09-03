"""Focused checks for the frozen E-Sim benchmark runner (#70)."""

import io
import json
from collections.abc import Mapping, Sequence
from typing import Self

import pytest

from retro_backend.esim.providers.base import ModelReply
from retro_backend.esim.rotation import RETRYABLE_ACTION_ERRORS
from retro_backend.esim.runner import RunnerConfig, run_case


class Replies:
    def __init__(self, replies: list[ModelReply]) -> None:
        self.replies = replies
        self.messages: list[list[dict[str, str]]] = []

    async def generate(
        self, messages: Sequence[Mapping[str, str]] | str, *, max_tokens: int | None = None
    ) -> ModelReply:
        del max_tokens
        self.messages.append([dict(message) for message in messages] if not isinstance(messages, str) else [])
        return self.replies.pop(0)

    async def close(self) -> None:
        return None

    async def __aenter__(self) -> Self:
        return self

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc_val: BaseException | None,
        exc_tb: object | None,
    ) -> None:
        await self.close()


def _reply(text: str | None, error: str | None = None) -> ModelReply:
    return ModelReply(text=text, input_tokens=3, output_tokens=2, latency_ms=4.0, error=error)


@pytest.mark.asyncio
async def test_runner_retries_malformed_json_then_applies_action() -> None:
    adapter = Replies([_reply("not json"), _reply('{"tool":"prepare","arguments":{"orderId":"order-2"}}')])
    output = io.StringIO()

    result = await run_case(
        RunnerConfig("dinner-rush-001", "nim:test", max_turns=2), adapter, jsonl=output
    )

    assert result.summary["invalidResponses"] == 1
    assert result.summary["acceptedActions"] == 1
    assert result.summary["termination"] == "turn_limit"
    records = [json.loads(line) for line in output.getvalue().splitlines()]
    assert records[0]["error"].startswith("invalid_json")
    assert records[1]["action"]["tool"] == "prepare"


@pytest.mark.asyncio
async def test_runner_retries_invalid_tool_call_for_same_role() -> None:
    adapter = Replies([
        _reply('{"tool":"seat","arguments":{"tableId":"table-3","partySize":2}}'),
        _reply('{"tool":"prepare","arguments":{"orderId":"order-2"}}'),
    ])

    result = await run_case(RunnerConfig("dinner-rush-001", "nim:test", max_turns=2), adapter)

    assert result.summary["rejectedActions"] == 1
    assert result.summary["acceptedActions"] == 1
    assert len(adapter.messages) == 2
    second_prompt = json.loads(adapter.messages[1][1]["content"])
    assert second_prompt["previousError"].startswith("role_not_permitted")


@pytest.mark.asyncio
async def test_runner_enforces_turn_limit_inside_a_retry_loop() -> None:
    adapter = Replies([_reply("not json"), _reply('{"tool":null}')])

    result = await run_case(RunnerConfig("dinner-rush-001", "nim:test", max_turns=1), adapter)

    assert result.summary["turns"] == 1
    assert result.summary["termination"] == "turn_limit"
    assert len(adapter.messages) == 1


@pytest.mark.asyncio
async def test_runner_completes_the_frozen_horizon() -> None:
    adapter = Replies([_reply('{"tool":null}') for _ in range(96)])

    result = await run_case(RunnerConfig("dinner-rush-001", "nim:test"), adapter)

    assert result.summary["termination"] == "episode_complete"
    assert result.summary["ticks"] == 24
    assert result.summary["turns"] == 96


@pytest.mark.asyncio
async def test_runner_retries_a_transient_provider_failure() -> None:
    adapter = Replies([_reply(None, "timeout: upstream timed out"), _reply('{"tool":null}')])

    result = await run_case(RunnerConfig("dinner-rush-001", "nim:test", max_turns=2), adapter)

    assert result.summary["modelErrors"] == 1
    assert result.summary["turns"] == 2
    assert result.summary["noActions"] == 1


def test_safety_rejections_are_not_retryable() -> None:
    assert "unsafe_action" not in RETRYABLE_ACTION_ERRORS
