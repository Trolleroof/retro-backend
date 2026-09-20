"""Focused checks for the frozen E-Sim benchmark runner (#70)."""

# Test doubles intentionally keep the adapter protocol loose; production code
# is checked strictly, while these helpers only need to model call behavior.
# pyright: reportImplicitOverride=false, reportMissingParameterType=false, reportUnknownParameterType=false

import asyncio
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


def _reply(text: str | None, error: str | None = None, reasoning: str | None = None) -> ModelReply:
    return ModelReply(text=text, input_tokens=3, output_tokens=2, latency_ms=4.0, error=error, reasoning=reasoning, reasoning_tokens=5 if reasoning else 0)


@pytest.mark.asyncio
async def test_runner_keeps_malformed_json_as_model_behavior() -> None:
    adapter = Replies([_reply("not json", reasoning="tried strict JSON")])
    output = io.StringIO()

    result = await run_case(
        RunnerConfig("dinner-rush-001", "nim:test", max_turns=1), adapter, jsonl=output
    )

    assert result.summary["invalidResponses"] == 1
    assert result.summary["acceptedActions"] == 0
    assert result.summary["runValidity"] == "valid"
    assert result.summary["termination"] == "turn_limit"
    records = [json.loads(line) for line in output.getvalue().splitlines()]
    assert records[0]["error"].startswith("invalid_json")
    assert records[0].get("providerError") is None
    assert records[0]["reasoning"] == "tried strict JSON"
    assert records[0]["reasoningTokens"] == 5


@pytest.mark.asyncio
async def test_runner_keeps_adapter_reported_malformed_completion_as_model_behavior() -> None:
    output = io.StringIO()
    result = await run_case(
        RunnerConfig("dinner-rush-001", "nim:test", max_turns=1),
        Replies([_reply(None, "malformed_response: invalid model completion")]),
        jsonl=output,
    )

    assert result.summary["runValidity"] == "valid"
    assert result.summary["invalidResponses"] == 1
    record = json.loads(output.getvalue().splitlines()[0])
    assert record["modelBehavior"] is True
    assert "providerError" not in record


@pytest.mark.asyncio
async def test_runner_keeps_illegal_action_as_model_behavior() -> None:
    adapter = Replies([_reply('{"tool":"seat","arguments":{"tableId":"table-3","partySize":2}}')])

    result = await run_case(RunnerConfig("dinner-rush-001", "nim:test", max_turns=1), adapter)

    assert result.summary["rejectedActions"] == 1
    assert result.summary["acceptedActions"] == 0
    assert len(adapter.messages) == 1


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


@pytest.mark.asyncio
async def test_runner_times_out_twice_then_marks_episode_provider_invalid() -> None:
    class SlowReplies(Replies):
        async def generate(self, messages, *, max_tokens=None):
            del messages, max_tokens
            await asyncio.sleep(0.02)
            return _reply('{"tool":null}')

    output = io.StringIO()
    result = await run_case(
        RunnerConfig(
            "dinner-rush-001",
            "nim:test",
            max_attempts=3,
            max_turns=10,
            request_timeout=0.001,
            retry_delay_seconds=0,
        ),
        SlowReplies([]),
        jsonl=output,
    )

    assert result.summary["runValidity"] == "provider_invalid"
    assert result.summary["termination"] == "provider_invalid"
    assert result.summary["turns"] == 3
    reason = result.summary["providerInvalidReason"]
    assert isinstance(reason, str)
    assert reason.startswith("timeout:")
    assert sum(1 for line in output.getvalue().splitlines() if '"providerError":true' in line) == 3


@pytest.mark.asyncio
async def test_runner_honors_bounded_retry_after() -> None:
    delays: list[float] = []
    original_sleep = asyncio.sleep

    async def capture_sleep(delay: float) -> None:
        delays.append(delay)

    asyncio.sleep = capture_sleep  # type: ignore[assignment]
    try:
        result = await run_case(
            RunnerConfig(
                "dinner-rush-001",
                "nim:test",
                max_turns=2,
                max_retry_delay_seconds=0.01,
                retry_delay_seconds=0.01,
            ),
            Replies([
                ModelReply(None, 0, 0, 1, "rate_limit: 429 Too Many Requests", retry_after_seconds=99),
                _reply('{"tool":null}'),
            ]),
        )
    finally:
        asyncio.sleep = original_sleep  # type: ignore[assignment]

    assert result.summary["runValidity"] == "valid"
    assert delays == [0.01]


@pytest.mark.asyncio
async def test_runner_retries_http_500_then_scores_success() -> None:
    result = await run_case(
        RunnerConfig("dinner-rush-001", "nim:test", max_turns=2, retry_delay_seconds=0),
        Replies([_reply(None, "http_error: 500 Internal Gateway Error"), _reply('{"tool":null}')]),
    )

    assert result.summary["runValidity"] == "valid"
    assert result.summary["modelErrors"] == 1
    assert result.summary["noActions"] == 1


@pytest.mark.asyncio
async def test_runner_records_all_failed_provider_attempts() -> None:
    output = io.StringIO()
    result = await run_case(
        RunnerConfig("dinner-rush-001", "nim:test", max_attempts=3, max_turns=3, retry_delay_seconds=0),
        Replies([_reply(None, "http_error: 500 one"), _reply(None, "http_error: 502 two"), _reply(None, "http_error: 503 three")]),
        jsonl=output,
    )

    records = [json.loads(line) for line in output.getvalue().splitlines()]
    assert result.summary["runValidity"] == "provider_invalid"
    assert [record["attempt"] for record in records if record["event"] == "model_attempt"] == [1, 2, 3]
    assert all(record.get("providerError") is True for record in records[:3])


def test_safety_rejections_are_not_retryable() -> None:
    assert "unsafe_action" not in RETRYABLE_ACTION_ERRORS


@pytest.mark.asyncio
async def test_a_truncated_reply_is_retried_with_the_error_as_feedback() -> None:
    """Truncation is retryable only because the retry is not identical.

    The runner must actually forward the truncation error as previousError on
    the next attempt -- that forwarding is the entire justification for
    treating truncated_response as retryable at all.
    """
    adapter = Replies(
        [
            _reply(None, "truncated_response: output token budget exhausted mid-answer"),
            _reply('{"tool":null}'),
        ]
    )

    result = await run_case(RunnerConfig("dinner-rush-001", "nim:test", max_turns=2), adapter)

    assert result.summary["modelErrors"] == 1
    assert result.summary["turns"] == 2
    second_request = adapter.messages[1]
    user_payload = json.loads(next(m["content"] for m in second_request if m["role"] == "user"))
    assert "truncated_response" in user_payload["previousError"]


@pytest.mark.asyncio
async def test_chef_server_host_are_told_the_real_worker_roster() -> None:
    """A live run had a model invent ``"manager"`` for request_help.

    Chef, server, and host observations carry no worker roster -- only
    expediter's does -- yet every role's tool set includes request_help and
    send_message, both of which need a real coworker id. Without a roster in
    the prompt those three roles cannot answer that argument except by
    guessing, and 121 of 128 rejected actions in one live run were exactly
    that guess repeating.
    """
    adapter = Replies([_reply('{"tool":null}') for _ in range(4)])

    await run_case(RunnerConfig("dinner-rush-001", "nim:test", max_turns=4), adapter)

    payloads = [
        json.loads(next(m["content"] for m in request if m["role"] == "user"))
        for request in adapter.messages
    ]
    chef, server, host, expediter = payloads

    for role_payload in (chef, server, host):
        assert role_payload["workerIds"] == {
            "chef": "worker-chef",
            "expediter": "worker-expediter",
            "host": "worker-host",
            "server": "worker-server",
        }

    # Expediter's own observation already names every worker; adding the same
    # ids again would just be a second copy of information already present.
    assert "workers" in expediter["observation"]
    assert "workerIds" not in expediter
