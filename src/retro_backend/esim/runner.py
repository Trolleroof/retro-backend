"""Run one frozen Restaurant E-Sim case against one model adapter (#70)."""

import json
from collections import Counter
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any, TextIO

from pydantic import ValidationError

from retro_backend.esim.case import load_case
from retro_backend.esim.contracts import ACTION_VERSION, ActionEnvelope, ActionError, Role
from retro_backend.esim.providers.base import ModelAdapter, ModelReply
from retro_backend.esim.providers.registry import get_adapter
from retro_backend.esim.rotation import (
    RETRYABLE_ACTION_ERRORS,
    retryable_model_error,
    role_rotation,
)
from retro_backend.esim.simulator import RestaurantSim
from retro_backend.esim.state_hash import state_hash
from retro_backend.esim.tools import ROLE_TOOLS, TOOL_ARGS

RUNNER_EPOCH = datetime(2026, 1, 1, tzinfo=UTC)


@dataclass(frozen=True, slots=True)
class RunnerConfig:
    case_id: str
    model: str
    max_attempts: int = 3
    max_output_tokens: int = 256
    max_turns: int | None = None

    def __post_init__(self) -> None:
        if self.max_attempts < 1 or self.max_output_tokens < 1:
            raise ValueError("max_attempts and max_output_tokens must be positive")
        if self.max_turns is not None and self.max_turns < 1:
            raise ValueError("max_turns must be positive when set")


@dataclass(frozen=True, slots=True)
class RunResult:
    summary: Mapping[str, object]


def _record(sink: TextIO | None, record: Mapping[str, object]) -> None:
    if sink is not None:
        sink.write(json.dumps(record, sort_keys=True, separators=(",", ":")) + "\n")


def _messages(
    role: Role, observation: Mapping[str, object], feedback: str | None
) -> list[dict[str, str]]:
    tools = {
        tool: TOOL_ARGS[tool].model_json_schema(by_alias=True)
        for tool in sorted(ROLE_TOOLS[role])
    }
    prompt = {
        "observation": observation,
        "tools": tools,
        "response": '{"tool":"tool_name","arguments":{...}} or {"tool":null}',
    }
    if feedback is not None:
        prompt["previousError"] = feedback
    return [
        {
            "role": "system",
            "content": "You control one restaurant worker. Reply with exactly one JSON object and no markdown.",
        },
        {"role": "user", "content": json.dumps(prompt, sort_keys=True)},
    ]


def _parse_action(
    text: str,
    *,
    episode_id: str,
    worker_id: str,
    role: Role,
    tick: int,
    attempt: int,
) -> ActionEnvelope | None:
    try:
        payload = json.loads(text)
    except json.JSONDecodeError as exc:
        raise ValueError(f"invalid_json: {exc.msg}") from exc
    if not isinstance(payload, dict) or set(payload) - {"tool", "arguments"}:
        raise ValueError("invalid_tool_call: expected only tool and arguments")
    tool = payload.get("tool")
    if tool is None:
        if set(payload) != {"tool"}:
            raise ValueError("invalid_tool_call: no-action response cannot include arguments")
        return None
    arguments = payload.get("arguments")
    if not isinstance(arguments, dict):
        raise ValueError("invalid_tool_call: arguments must be an object")
    request_id = f"model-{role}-t{tick:04d}-a{attempt:02d}"
    try:
        return ActionEnvelope(
            version=ACTION_VERSION,
            episode_id=episode_id,
            actor_id=worker_id,
            actor_type="worker",
            role=role,
            tool=tool,
            arguments=arguments,
            tick=tick,
            timestamp=RUNNER_EPOCH + timedelta(minutes=tick),
            request_id=request_id,
            idempotency_key=f"{episode_id}:{request_id}",
        )
    except ValidationError as exc:
        raise ValueError(f"invalid_tool_call: {exc.errors()[0]['msg']}") from exc


async def run_case(
    config: RunnerConfig, adapter: ModelAdapter, *, jsonl: TextIO | None = None
) -> RunResult:
    """Run one frozen case and write auditable per-attempt records to ``jsonl``."""
    case = load_case(config.case_id)
    sim = RestaurantSim(case.initial_state, event_schedule=case.event_schedule)
    state = sim.reset(case.seed)
    max_turns = config.max_turns or state.episode.max_ticks * 4
    totals = Counter[str]()
    latency_ms = 0.0
    termination = "episode_complete"

    while state.episode.phase == "active":
        for role, worker_id in role_rotation(state):
            if totals["turns"] >= max_turns:
                termination = "turn_limit"
                break
            feedback: str | None = None
            for attempt in range(1, config.max_attempts + 1):
                if totals["turns"] >= max_turns:
                    termination = "turn_limit"
                    break
                observation = sim.observe(role).model_dump(by_alias=True)
                reply: ModelReply = await adapter.generate(
                    _messages(role, observation, feedback), max_tokens=config.max_output_tokens
                )
                totals["turns"] += 1
                totals["input_tokens"] += reply.input_tokens
                totals["output_tokens"] += reply.output_tokens
                latency_ms += reply.latency_ms
                record: dict[str, object] = {
                    "event": "model_attempt",
                    "role": role,
                    "tick": state.episode.tick,
                    "attempt": attempt,
                    "latencyMs": reply.latency_ms,
                    "inputTokens": reply.input_tokens,
                    "outputTokens": reply.output_tokens,
                }
                if not reply.ok:
                    error = reply.error or "unknown_model_error"
                    totals["model_errors"] += 1
                    record["error"] = error
                    _record(jsonl, record)
                    if retryable_model_error(error) and attempt < config.max_attempts:
                        feedback = error
                        continue
                    break
                record["response"] = reply.text or ""
                try:
                    action = _parse_action(
                        reply.text or "", episode_id=state.episode.id, worker_id=worker_id,
                        role=role, tick=state.episode.tick, attempt=attempt,
                    )
                except ValueError as exc:
                    feedback = str(exc)
                    totals["invalid_responses"] += 1
                    record["error"] = feedback
                    _record(jsonl, record)
                    if attempt < config.max_attempts:
                        continue
                    break
                if action is None:
                    totals["no_actions"] += 1
                    record["action"] = None
                    _record(jsonl, record)
                    break
                result = sim.act(action)
                record["action"] = action.model_dump(by_alias=True, mode="json")
                if isinstance(result, ActionError):
                    feedback = f"{result.code}: {result.message}"
                    totals["rejected_actions"] += 1
                    record["actionError"] = result.model_dump(by_alias=True)
                    _record(jsonl, record)
                    if result.code in RETRYABLE_ACTION_ERRORS and attempt < config.max_attempts:
                        continue
                    break
                state = result
                totals["accepted_actions"] += 1
                _record(jsonl, record)
                break
            if termination == "turn_limit":
                break
        if termination == "turn_limit":
            break
        state = sim.advance_time()

    summary: dict[str, object] = {
        "event": "summary",
        "caseId": config.case_id,
        "model": config.model,
        "termination": termination,
        "ticks": state.episode.tick,
        "turns": totals["turns"],
        "acceptedActions": totals["accepted_actions"],
        "rejectedActions": totals["rejected_actions"],
        "noActions": totals["no_actions"],
        "invalidResponses": totals["invalid_responses"],
        "modelErrors": totals["model_errors"],
        "inputTokens": totals["input_tokens"],
        "outputTokens": totals["output_tokens"],
        "latencyMs": latency_ms,
        "outcomes": state.outcomes.model_dump(by_alias=True),
        "finalStateHash": state_hash(state.model_dump(by_alias=True)),
    }
    _record(jsonl, summary)
    return RunResult(summary=summary)


async def run_model_case(
    config: RunnerConfig, *, jsonl: TextIO | None = None, **provider_kwargs: Any
) -> RunResult:
    """Construct the configured provider adapter, run, then close it."""
    adapter = get_adapter(config.model, **provider_kwargs)
    async with adapter:
        return await run_case(config, adapter, jsonl=jsonl)
