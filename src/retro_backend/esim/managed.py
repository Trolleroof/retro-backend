"""The managed round loop: one manager model over a fixed worker model (#75).

:func:`~retro_backend.esim.runner.run_case` ranks a model by having it answer
as all four workers. This module runs the shape a human manager plays instead,
and it is the loop the manager benchmark is scored from:

    reset -> manager observation -> manager action -> four worker decisions
          -> routine simulation interval -> next round -> grade

**Manager identity and worker identity are separate.** They are two adapters,
two configuration fields, two served-model sets, and two sets of records. The
comparison this supports is *between manager models with worker identity held
fixed* -- swap the manager, keep the staff, and the difference in the grade is
attributable to management. Reporting one "model" for such a run would be a
false attribution, so nothing here does.

**Twelve rounds, and the budget that follows from them.** A complete level is
:data:`DEFAULT_ROUNDS` rounds: 12 manager requests and 48 worker requests
before retries, with the episode's ticks divided evenly between the rounds as
the routine simulation interval. Retries multiply both budgets by
``max_attempts`` and are counted separately per actor, so a manager cannot
consume the workers' allowance or the reverse.

**Nothing scripted is ever recorded as a model decision.** Every action in a
managed run comes from one of the two adapters. When an adapter fails or
returns something unusable, the run records that failure and the round moves
on -- it does not substitute a policy action, and the summary marks the run
``provider_failure`` so it is never read as a clean model result.
"""

import json
from collections import Counter
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Any, TextIO

from pydantic import ValidationError

from retro_backend.esim.case import ENV_VERSION, load_case
from retro_backend.esim.contracts import ActionError, RestaurantState, Role
from retro_backend.esim.manager import (
    MANAGER_ACTOR_ID,
    MANAGER_TOOL_ARGS,
    DirectiveBoard,
    ManagerActionEnvelope,
    StandingDirective,
    WorkerActivity,
    observe_manager,
)
from retro_backend.esim.providers.base import ModelAdapter, ModelReply
from retro_backend.esim.providers.registry import get_adapter, parse_model_spec
from retro_backend.esim.rotation import (
    RETRYABLE_ACTION_ERRORS,
    ROLE_ORDER,
    retryable_model_error,
    role_rotation,
)
from retro_backend.esim.runner import (
    RunResult,
    build_worker_messages,
    parse_worker_action,
    record_jsonl,
)
from retro_backend.esim.simulator import RestaurantSim
from retro_backend.esim.state_hash import state_hash
from retro_backend.esim.trajectory import (
    ManagerTurnOutcome,
    Trajectory,
    TrajectoryRecorder,
)

# The frozen level length: 12 rounds, so 12 manager and 48 worker requests
# before retries. Changing this changes what "a complete level" means and is a
# manifest revision, not a flag to reach for per run.
DEFAULT_ROUNDS = 12

MANAGED_SHAPE = "one manager model directs four workers on a fixed worker model"

__all__ = [
    "DEFAULT_ROUNDS",
    "MANAGED_SHAPE",
    "ManagedRunnerConfig",
    "manager_turn_budget",
    "parse_manager_action",
    "run_managed_case",
    "run_managed_model_case",
    "ticks_per_round",
    "worker_turn_budget",
]


@dataclass(frozen=True, slots=True)
class ManagedRunnerConfig:
    """One managed run's configuration. The two models never share a field."""

    case_id: str
    manager_model: str
    worker_model: str
    rounds: int = DEFAULT_ROUNDS
    max_attempts: int = 3
    max_output_tokens: int = 2500
    # Per-request wall clock, seconds. A manager turn carries the whole tool
    # surface and a shift to read, and a reasoning model can spend longer on
    # that than the adapter's 30s default allows -- which shows up as a
    # timeout, not as a slow answer, and costs the round its decision. Left as
    # None to keep the adapter's own default rather than pinning a second copy
    # of it here.
    request_timeout: float | None = None

    def __post_init__(self) -> None:
        if self.max_attempts < 1 or self.max_output_tokens < 1:
            raise ValueError("max_attempts and max_output_tokens must be positive")
        if self.rounds < 1:
            raise ValueError("rounds must be positive")
        if self.request_timeout is not None and self.request_timeout <= 0:
            raise ValueError("request_timeout must be positive when set")


def ticks_per_round(max_ticks: int, rounds: int) -> int:
    """The routine simulation interval between two manager turns.

    Required to divide evenly. An uneven split would give some rounds more
    restaurant than others, so the same manager decision would be worth more
    or less depending on where in the episode it landed -- and the leftover
    ticks would have to be run either under the last round's directives or
    under none, both of which are a different experiment from the one the
    manifest describes.
    """
    if rounds > max_ticks:
        raise ValueError(
            f"{rounds} rounds do not fit in a {max_ticks}-tick episode"
        )
    if max_ticks % rounds:
        raise ValueError(
            f"{max_ticks} ticks do not divide evenly into {rounds} rounds;"
            + " choose a round count that divides the case horizon"
        )
    return max_ticks // rounds


def manager_turn_budget(config: ManagedRunnerConfig) -> int:
    """One manager request per round, times the retry allowance."""
    return config.rounds * config.max_attempts


def worker_turn_budget(config: ManagedRunnerConfig) -> int:
    """Four worker requests per round, times the retry allowance."""
    return config.rounds * len(ROLE_ORDER) * config.max_attempts


def _manager_messages(
    observation: Mapping[str, object], feedback: str | None
) -> list[dict[str, str]]:
    """The manager prompt. Same discipline as the worker prompt, same shape.

    The manager is told what it runs and what it may write, and nothing else:
    no grader weights, no probe definitions, no event schedule, no score.
    """
    tools = {
        tool: MANAGER_TOOL_ARGS[tool].model_json_schema(by_alias=True)
        for tool in sorted(MANAGER_TOOL_ARGS)
    }
    prompt: dict[str, object] = {
        "observation": observation,
        "tools": tools,
        "response": '{"tool":"tool_name","arguments":{...}} or {"tool":null}',
    }
    if feedback is not None:
        prompt["previousError"] = feedback
    return [
        {
            "role": "system",
            "content": (
                "You are the manager of a restaurant shift. You do not touch "
                "orders, tables, or equipment yourself: you post standing "
                "instructions that your chef, server, host, and expediter "
                "read before every decision. Reply with exactly one JSON "
                "object and nothing else: no markdown, no explanation, no "
                "reasoning before or after it. Keep the JSON itself short: "
                "one tool call, minimal argument values."
            ),
        },
        {"role": "user", "content": json.dumps(prompt, sort_keys=True, separators=(",", ":"))},
    ]


def parse_manager_action(
    text: str,
    *,
    episode_id: str,
    round_number: int,
    tick: int,
    attempt: int,
) -> ManagerActionEnvelope | None:
    """Read one manager reply into an envelope, or ``None`` for no action.

    Mirrors :func:`~retro_backend.esim.runner.parse_worker_action` down to the
    error strings, so a manager and a worker that make the same format mistake
    are told the same thing and counted the same way.
    """
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
    try:
        return ManagerActionEnvelope(
            episode_id=episode_id,
            tool=tool,
            arguments=arguments,
            round=round_number,
            tick=tick,
            request_id=f"manager-r{round_number:03d}-a{attempt:02d}",
        )
    except ValidationError as exc:
        raise ValueError(f"invalid_tool_call: {exc.errors()[0]['msg']}") from exc


@dataclass(slots=True)
class _ActorTally:
    """Per-actor usage. Kept apart so neither model's numbers absorb the other's."""

    totals: Counter[str]
    served_models: set[str]
    latency_ms: float = 0.0

    @classmethod
    def empty(cls) -> "_ActorTally":
        return cls(totals=Counter[str](), served_models=set())

    def observe_reply(self, reply: ModelReply) -> None:
        self.totals["turns"] += 1
        if reply.ok:
            self.totals["ok_replies"] += 1
        self.totals["input_tokens"] += reply.input_tokens
        self.totals["output_tokens"] += reply.output_tokens
        self.latency_ms += reply.latency_ms
        if reply.served_model is not None:
            self.served_models.add(reply.served_model)

    def mismatch(self, requested_spec: str) -> bool:
        # parse_model_spec drops the provider prefix; the provider answers with
        # a bare model id, so compare like with like before calling it a
        # mismatch.
        requested = (
            parse_model_spec(requested_spec)[1]
            if ":" in requested_spec
            else requested_spec
        )
        return bool(self.served_models) and self.served_models != {requested}

    def unverified(self) -> bool:
        """This actor answered, but never said which model did.

        A provider may return a body with no usable ``model`` field, and an
        empty served-model set cannot mismatch anything -- so without this the
        run would report ``valid`` having confirmed nothing about who answered.
        On a shape whose whole claim is "this manager, those workers", an
        unconfirmed identity is not a clean result.
        """
        return bool(self.totals["ok_replies"]) and not self.served_models


async def run_managed_case(
    config: ManagedRunnerConfig,
    *,
    manager_adapter: ModelAdapter,
    worker_adapter: ModelAdapter,
    jsonl: TextIO | None = None,
) -> RunResult:
    """Run one frozen case as a managed shift and record every decision.

    The loop is one round at a time: the manager sees the shift and posts or
    withdraws a standing instruction, each of the four workers then decides
    under whatever now stands, and the simulator runs the routine interval
    before the next round opens.
    """
    case = load_case(config.case_id)
    sim = RestaurantSim(case.initial_state, event_schedule=case.event_schedule)
    state = sim.reset(case.seed)
    interval = ticks_per_round(state.episode.max_ticks, config.rounds)
    recorder = TrajectoryRecorder(
        case_id=case.case_id,
        seed=case.seed,
        env_version=ENV_VERSION,
        contract_version=case.contract_version,
        event_schedule=case.event_schedule,
    )
    recorder.record_state(state)

    board = DirectiveBoard()
    manager = _ActorTally.empty()
    workers = _ActorTally.empty()
    max_manager_turns = manager_turn_budget(config)
    max_worker_turns = worker_turn_budget(config)
    termination = "episode_complete"
    rounds_played = 0
    last_round: tuple[WorkerActivity, ...] = ()

    for round_number in range(1, config.rounds + 1):
        if state.episode.phase != "active":
            break
        if (
            manager.totals["turns"] >= max_manager_turns
            or workers.totals["turns"] >= max_worker_turns
        ):
            termination = "turn_limit"
            break
        rounds_played = round_number

        await _run_manager_turn(
            config,
            manager_adapter,
            state=state,
            board=board,
            tally=manager,
            recorder=recorder,
            jsonl=jsonl,
            round_number=round_number,
            rounds_total=config.rounds,
            last_round=last_round,
            max_turns=max_manager_turns,
        )

        activity, state, hit_limit = await _run_worker_round(
            config,
            worker_adapter,
            sim=sim,
            state=state,
            board=board,
            tally=workers,
            recorder=recorder,
            jsonl=jsonl,
            round_number=round_number,
            max_turns=max_worker_turns,
        )
        last_round = activity
        if hit_limit:
            termination = "turn_limit"
            break

        # The routine simulation interval: the restaurant runs on under the
        # instructions that stand, and the manager sees the result next round.
        for _ in range(interval):
            if state.episode.phase != "active":
                break
            state = sim.advance_time()
            recorder.record_state(state)

    recorder.record_final_directives(board.active())
    trajectory = recorder.finish(termination)
    summary = _summary(
        config,
        trajectory=trajectory,
        state=state,
        manager=manager,
        workers=workers,
        board=board,
        interval=interval,
        rounds_played=rounds_played,
    )
    record_jsonl(jsonl, summary)
    return RunResult(summary=summary, trajectory=trajectory)


async def _run_manager_turn(
    config: ManagedRunnerConfig,
    adapter: ModelAdapter,
    *,
    state: RestaurantState,
    board: DirectiveBoard,
    tally: _ActorTally,
    recorder: TrajectoryRecorder,
    jsonl: TextIO | None,
    round_number: int,
    rounds_total: int,
    last_round: Sequence[WorkerActivity],
    max_turns: int,
) -> None:
    """One manager turn, retries included. Always leaves exactly one record."""
    observation = observe_manager(
        state,
        round_number=round_number,
        rounds_total=rounds_total,
        directives=board.active(),
        last_round=last_round,
    ).model_dump(by_alias=True, mode="json")

    feedback: str | None = None
    envelope: ManagerActionEnvelope | None = None
    result: StandingDirective | None | ActionError = None
    outcome: ManagerTurnOutcome = "no_decision"

    for attempt in range(1, config.max_attempts + 1):
        if tally.totals["turns"] >= max_turns:
            break
        reply: ModelReply = await adapter.generate(
            _manager_messages(observation, feedback),
            max_tokens=config.max_output_tokens,
        )
        tally.observe_reply(reply)
        record: dict[str, object] = {
            "event": "model_attempt",
            "actor": "manager",
            "model": config.manager_model,
            "role": "manager",
            "round": round_number,
            "tick": state.episode.tick,
            "attempt": attempt,
            "servedModel": reply.served_model,
            "latencyMs": reply.latency_ms,
            "inputTokens": reply.input_tokens,
            "outputTokens": reply.output_tokens,
        }
        if not reply.ok:
            error = reply.error or "unknown_model_error"
            tally.totals["model_errors"] += 1
            record["error"] = error
            record_jsonl(jsonl, record)
            if retryable_model_error(error) and attempt < config.max_attempts:
                feedback = error
                continue
            break

        record["response"] = reply.text or ""
        try:
            envelope = parse_manager_action(
                reply.text or "",
                episode_id=state.episode.id,
                round_number=round_number,
                tick=state.episode.tick,
                attempt=attempt,
            )
        except ValueError as exc:
            feedback = str(exc)
            tally.totals["invalid_responses"] += 1
            record["error"] = feedback
            record_jsonl(jsonl, record)
            envelope = None
            if attempt < config.max_attempts:
                continue
            break

        if envelope is None:
            tally.totals["no_actions"] += 1
            outcome = "stood_down"
            record["action"] = None
            record_jsonl(jsonl, record)
            break

        result = board.apply(envelope)
        record["action"] = envelope.model_dump(by_alias=True, mode="json")
        if isinstance(result, ActionError):
            feedback = f"{result.code}: {result.message}"
            tally.totals["rejected_actions"] += 1
            outcome = "rejected"
            record["actionError"] = result.model_dump(by_alias=True)
            record_jsonl(jsonl, record)
            if attempt < config.max_attempts:
                envelope, result, outcome = None, None, "no_decision"
                continue
            break

        outcome = "applied"
        tally.totals["accepted_actions"] += 1
        record_jsonl(jsonl, record)
        break

    recorder.record_manager_action(
        envelope,
        result,
        outcome=outcome,
        round_number=round_number,
        tick=state.episode.tick,
        actor_id=MANAGER_ACTOR_ID,
        directives_digest=board.digest(),
    )


async def _run_worker_round(
    config: ManagedRunnerConfig,
    adapter: ModelAdapter,
    *,
    sim: RestaurantSim,
    state: RestaurantState,
    board: DirectiveBoard,
    tally: _ActorTally,
    recorder: TrajectoryRecorder,
    jsonl: TextIO | None,
    round_number: int,
    max_turns: int,
) -> tuple[tuple[WorkerActivity, ...], RestaurantState, bool]:
    """One decision from each of the four workers, in the fixed role order.

    Returns what each worker did (for the manager's next observation), the
    state after the round, and whether the worker turn budget ran out.
    """
    digest = board.digest()
    worker_ids: dict[Role, str] = {worker.role: worker.id for worker in state.workers}
    activity: list[WorkerActivity] = []

    for role, worker_id in role_rotation(state):
        if tally.totals["turns"] >= max_turns:
            return tuple(activity), state, True
        outcome = WorkerActivity(role=role, worker_id=worker_id)
        feedback: str | None = None

        for attempt in range(1, config.max_attempts + 1):
            if tally.totals["turns"] >= max_turns:
                activity.append(outcome)
                return tuple(activity), state, True
            observation = sim.observe(role).model_dump(by_alias=True)
            reply: ModelReply = await adapter.generate(
                build_worker_messages(
                    role,
                    observation,
                    feedback,
                    worker_ids=worker_ids,
                    directives=board.for_role(role),
                ),
                max_tokens=config.max_output_tokens,
            )
            tally.observe_reply(reply)
            record: dict[str, object] = {
                "event": "model_attempt",
                "actor": "worker",
                "model": config.worker_model,
                "role": role,
                "round": round_number,
                "tick": state.episode.tick,
                "attempt": attempt,
                "directivesDigest": digest,
                "servedModel": reply.served_model,
                "latencyMs": reply.latency_ms,
                "inputTokens": reply.input_tokens,
                "outputTokens": reply.output_tokens,
            }
            if not reply.ok:
                error = reply.error or "unknown_model_error"
                tally.totals["model_errors"] += 1
                record["error"] = error
                record_jsonl(jsonl, record)
                if retryable_model_error(error) and attempt < config.max_attempts:
                    feedback = error
                    continue
                break

            record["response"] = reply.text or ""
            try:
                action = parse_worker_action(
                    reply.text or "",
                    episode_id=state.episode.id,
                    worker_id=worker_id,
                    role=role,
                    tick=state.episode.tick,
                    attempt=attempt,
                )
            except ValueError as exc:
                feedback = str(exc)
                tally.totals["invalid_responses"] += 1
                record["error"] = feedback
                record_jsonl(jsonl, record)
                if attempt < config.max_attempts:
                    continue
                break

            if action is None:
                tally.totals["no_actions"] += 1
                record["action"] = None
                record_jsonl(jsonl, record)
                break

            result = sim.act(action)
            recorder.record_action(
                action, result, round_number=round_number, directives_digest=digest
            )
            record["action"] = action.model_dump(by_alias=True, mode="json")
            outcome = WorkerActivity(
                role=role,
                worker_id=worker_id,
                tool=action.tool,
                accepted=not isinstance(result, ActionError),
                error_code=result.code if isinstance(result, ActionError) else None,
            )
            if isinstance(result, ActionError):
                feedback = f"{result.code}: {result.message}"
                tally.totals["rejected_actions"] += 1
                record["actionError"] = result.model_dump(by_alias=True)
                record_jsonl(jsonl, record)
                if result.code in RETRYABLE_ACTION_ERRORS and attempt < config.max_attempts:
                    continue
                break

            state = result
            tally.totals["accepted_actions"] += 1
            record_jsonl(jsonl, record)
            break

        activity.append(outcome)

    return tuple(activity), state, False


def _summary(
    config: ManagedRunnerConfig,
    *,
    trajectory: Trajectory,
    state: RestaurantState,
    manager: _ActorTally,
    workers: _ActorTally,
    board: DirectiveBoard,
    interval: int,
    rounds_played: int,
) -> dict[str, object]:
    """The run summary, with the two models' numbers reported side by side.

    ``runValidity`` is the field that keeps a degraded run from being read as
    a result, and it has three values rather than two.

    ``provider_failure`` -- an adapter error, or a reply served by a model
    other than the one requested. The scores describe a run that did not get
    the models it asked for.

    ``unverified_identity`` -- every call succeeded, but a provider never named
    the model that answered, so which model produced these scores is not
    established. Kept apart from ``provider_failure`` because nothing failed;
    kept apart from ``valid`` because attribution is the claim this shape
    makes, and an unconfirmed one is not a result to report.
    """
    manager_mismatch = manager.mismatch(config.manager_model)
    worker_mismatch = workers.mismatch(config.worker_model)
    failed = (
        manager_mismatch
        or worker_mismatch
        or bool(manager.totals["model_errors"])
        or bool(workers.totals["model_errors"])
    )
    unverified = manager.unverified() or workers.unverified()
    validity = (
        "provider_failure" if failed else "unverified_identity" if unverified else "valid"
    )
    return {
        "event": "summary",
        "shape": MANAGED_SHAPE,
        "caseId": config.case_id,
        "managerModel": config.manager_model,
        "workerModel": config.worker_model,
        "rounds": config.rounds,
        "roundsPlayed": rounds_played,
        "requestTimeoutSeconds": config.request_timeout,
        "ticksPerRound": interval,
        "termination": trajectory.termination,
        "ticks": state.episode.tick,
        "runValidity": validity,
        "manager": {
            "servedModels": sorted(manager.served_models),
            "servedModelMismatch": manager_mismatch,
            "servedModelUnverified": manager.unverified(),
            "turns": manager.totals["turns"],
            "directivesAccepted": manager.totals["accepted_actions"],
            "directivesRejected": manager.totals["rejected_actions"],
            "noActions": manager.totals["no_actions"],
            "invalidResponses": manager.totals["invalid_responses"],
            "modelErrors": manager.totals["model_errors"],
            "inputTokens": manager.totals["input_tokens"],
            "outputTokens": manager.totals["output_tokens"],
            "latencyMs": manager.latency_ms,
        },
        "workers": {
            "servedModels": sorted(workers.served_models),
            "servedModelMismatch": worker_mismatch,
            "servedModelUnverified": workers.unverified(),
            "turns": workers.totals["turns"],
            "acceptedActions": workers.totals["accepted_actions"],
            "rejectedActions": workers.totals["rejected_actions"],
            "noActions": workers.totals["no_actions"],
            "invalidResponses": workers.totals["invalid_responses"],
            "modelErrors": workers.totals["model_errors"],
            "inputTokens": workers.totals["input_tokens"],
            "outputTokens": workers.totals["output_tokens"],
            "latencyMs": workers.latency_ms,
        },
        "finalDirectives": [
            directive.model_dump(by_alias=True, mode="json")
            for directive in board.active()
        ],
        "finalDirectivesDigest": board.digest(),
        "outcomes": state.outcomes.model_dump(by_alias=True),
        "finalStateHash": state_hash(state.model_dump(by_alias=True)),
    }


async def run_managed_model_case(
    config: ManagedRunnerConfig, *, jsonl: TextIO | None = None, **provider_kwargs: Any
) -> RunResult:
    """Construct both adapters, run the managed case, then close them.

    Two adapters are always constructed, even when the two specs are equal: a
    manager and a worker share no rate limit, no connection, and no
    conversation, and reusing one object for both would make the run depend on
    whether the operator happened to name the same model twice.
    """
    if config.request_timeout is not None:
        provider_kwargs.setdefault("timeout", config.request_timeout)
    manager_adapter = get_adapter(config.manager_model, **provider_kwargs)
    worker_adapter = get_adapter(config.worker_model, **provider_kwargs)
    async with manager_adapter, worker_adapter:
        return await run_managed_case(
            config,
            manager_adapter=manager_adapter,
            worker_adapter=worker_adapter,
            jsonl=jsonl,
        )
