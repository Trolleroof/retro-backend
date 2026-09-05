"""Run one frozen Restaurant E-Sim case against one model adapter (#70).

The run also records a :class:`~retro_backend.esim.trajectory.Trajectory`
(#71). The JSONL sink stays what it was -- an auditable per-attempt log,
including the model text -- but it is a stream, not an object, and the
deterministic grader needs an object: per-tick states, the accepted and
rejected action log, and the incidents that fired. Recording is free of the
model loop and free of grading; nothing here scores anything.
"""

import json
from collections import Counter
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any, TextIO

from pydantic import ValidationError

from retro_backend.esim.case import ENV_VERSION, load_case
from retro_backend.esim.contracts import ACTION_VERSION, ActionEnvelope, ActionError, Role
from retro_backend.esim.providers.base import ModelAdapter, ModelReply
from retro_backend.esim.providers.registry import get_adapter, parse_model_spec
from retro_backend.esim.rotation import (
    RETRYABLE_ACTION_ERRORS,
    ROLE_ORDER,
    retryable_model_error,
    role_rotation,
)
from retro_backend.esim.simulator import RestaurantSim
from retro_backend.esim.state_hash import state_hash
from retro_backend.esim.tools import ROLE_TOOLS, TOOL_ARGS
from retro_backend.esim.trajectory import Trajectory, TrajectoryRecorder

RUNNER_EPOCH = datetime(2026, 1, 1, tzinfo=UTC)


@dataclass(frozen=True, slots=True)
class RunnerConfig:
    case_id: str
    model: str
    max_attempts: int = 3
    # A reasoning model spends this budget thinking before it writes anything,
    # so a budget sized for the answer alone truncates every such model into a
    # failed turn. 1024 still truncated a subset of turns in live runs; 2500
    # gives a verbose reasoner enough room without materially widening the
    # turn-budget math in run_case (max_turns is independent of this value).
    max_output_tokens: int = 2500
    max_turns: int | None = None

    def __post_init__(self) -> None:
        if self.max_attempts < 1 or self.max_output_tokens < 1:
            raise ValueError("max_attempts and max_output_tokens must be positive")
        if self.max_turns is not None and self.max_turns < 1:
            raise ValueError("max_turns must be positive when set")


@dataclass(frozen=True, slots=True)
class RunResult:
    summary: Mapping[str, object]
    # Everything the grader is allowed to read. Kept beside the summary
    # rather than folded into it: the summary is a human-readable JSONL
    # record, the trajectory is a typed artifact that round-trips to disk.
    trajectory: Trajectory


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
            # Explicitly forbidding narrated reasoning matters for models that
            # think in the same content channel their answer goes in: without
            # it, a turn can spend its whole budget explaining before it ever
            # writes the tool call, which is the exact failure this line is
            # meant to prevent, not just clean prose off of.
            "content": (
                "You control one restaurant worker. Reply with exactly one "
                "JSON object and nothing else: no markdown, no explanation, "
                "no reasoning before or after it. Keep the JSON itself short: "
                "one tool call, minimal argument values."
            ),
        },
        # Compact separators shave input tokens on every turn; they do not
        # touch the model's own output budget, but a smaller prompt is a
        # smaller thing to reason about before answering it.
        {"role": "user", "content": json.dumps(prompt, sort_keys=True, separators=(",", ":"))},
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
    recorder = TrajectoryRecorder(
        case_id=case.case_id,
        seed=case.seed,
        env_version=ENV_VERSION,
        contract_version=case.contract_version,
        event_schedule=case.event_schedule,
    )
    recorder.record_state(state)
    # Every adapter call counts as a turn, retries included, and a full episode
    # needs max_ticks * len(ROLE_ORDER) *successful* turns. Budgeting exactly
    # that many made the zero-retry path the only one that could ever reach
    # "episode_complete": a single retry anywhere guaranteed "turn_limit", and
    # with it a completion score that measured the budget rather than the
    # model. The retry allowance has to be part of the budget.
    max_turns = config.max_turns or state.episode.max_ticks * len(ROLE_ORDER) * config.max_attempts
    totals = Counter[str]()
    latency_ms = 0.0
    termination = "episode_complete"
    # Every distinct id the provider claimed answered. More than one, or one
    # that is not the requested model, means the run is not the single-model
    # run the manifest says it is.
    served_models: set[str] = set()

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
                if reply.served_model is not None:
                    served_models.add(reply.served_model)
                record: dict[str, object] = {
                    "event": "model_attempt",
                    "role": role,
                    "tick": state.episode.tick,
                    "attempt": attempt,
                    "servedModel": reply.served_model,
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
                recorder.record_action(action, result)
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
        recorder.record_state(state)

    trajectory = recorder.finish(termination)
    # parse_model_spec drops the provider prefix; the provider answers with a
    # bare model id, so compare like with like before calling it a mismatch.
    requested = parse_model_spec(config.model)[1] if ":" in config.model else config.model
    summary: dict[str, object] = {
        "event": "summary",
        "caseId": config.case_id,
        "model": config.model,
        "servedModels": sorted(served_models),
        "servedModelMismatch": bool(served_models) and served_models != {requested},
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
    return RunResult(summary=summary, trajectory=trajectory)


async def run_model_case(
    config: RunnerConfig, *, jsonl: TextIO | None = None, **provider_kwargs: Any
) -> RunResult:
    """Construct the configured provider adapter, run, then close it."""
    adapter = get_adapter(config.model, **provider_kwargs)
    async with adapter:
        return await run_case(config, adapter, jsonl=jsonl)
