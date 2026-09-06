"""The managed run's contract: two identities, one instruction path (#75).

These are the offline mechanics checks. Every model here is a stub, so nothing
in this file is a benchmark result -- what it demonstrates is that a manager
model and a worker model stay separable, that a manager instruction actually
reaches the worker that is meant to follow it, and that a run which loses its
provider is not recorded as a clean one.
"""

import io
import json
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Self

import pytest

from retro_backend.esim.case import load_case
from retro_backend.esim.cli import (
    MANAGED_BUNDLE_VERSION,
    build_managed_manifest,
    build_parser,
    main,
    write_managed_bundle,
)
from retro_backend.esim.grader import grade
from retro_backend.esim.managed import (
    DEFAULT_ROUNDS,
    ManagedRunnerConfig,
    manager_turn_budget,
    parse_manager_action,
    run_managed_case,
    ticks_per_round,
    worker_turn_budget,
)
from retro_backend.esim.manager import (
    IMPATIENT_WAIT_MINUTES,
    DirectiveBoard,
    ManagerActionEnvelope,
    StandingDirective,
    observe_manager,
)
from retro_backend.esim.providers.base import ERR_TIMEOUT, ModelAdapter, ModelReply
from retro_backend.esim.providers.registry import register_provider
from retro_backend.esim.trajectory import Trajectory

CASE = "dinner-rush-001"


class Replies:
    """A stub adapter that answers from a script and remembers its prompts."""

    def __init__(self, replies: Sequence[ModelReply], *, served: str = "vendor/m") -> None:
        self.replies = list(replies)
        self.served = served
        self.prompts: list[list[dict[str, str]]] = []

    async def generate(
        self, messages: Sequence[Mapping[str, str]] | str, *, max_tokens: int | None = None
    ) -> ModelReply:
        del max_tokens
        if not isinstance(messages, str):
            self.prompts.append([dict(message) for message in messages])
        if self.replies:
            return self.replies.pop(0)
        return _reply('{"tool":null}', served=self.served)

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


def _reply(
    text: str | None, error: str | None = None, served: str | None = "vendor/m"
) -> ModelReply:
    return ModelReply(
        text=text,
        input_tokens=3,
        output_tokens=2,
        latency_ms=4.0,
        error=error,
        served_model=served if error is None else None,
    )


def _config(**overrides: object) -> ManagedRunnerConfig:
    base: dict[str, object] = {
        "case_id": CASE,
        "manager_model": "nim:vendor/boss",
        "worker_model": "nim:vendor/staff",
        "rounds": 2,
    }
    return ManagedRunnerConfig(**{**base, **overrides})  # pyright: ignore[reportArgumentType]


def _user_prompt(messages: list[dict[str, str]]) -> dict[str, object]:
    return json.loads(messages[1]["content"])


# -- the round contract -------------------------------------------------


def test_twelve_rounds_is_forty_eight_worker_and_twelve_manager_requests() -> None:
    config = ManagedRunnerConfig(
        case_id=CASE, manager_model="nim:a", worker_model="nim:b", max_attempts=1
    )

    assert config.rounds == DEFAULT_ROUNDS == 12
    assert manager_turn_budget(config) == 12
    assert worker_turn_budget(config) == 48
    # The frozen case's horizon divides evenly into the frozen round count,
    # so each round is followed by the same routine interval.
    assert ticks_per_round(load_case(CASE).initial_state.episode.max_ticks, 12) == 2


def test_a_round_count_that_does_not_divide_the_horizon_is_refused() -> None:
    # Silently running a short last round would make a late directive worth
    # less than an early one for reasons nothing records.
    with pytest.raises(ValueError, match="divide evenly"):
        ticks_per_round(24, 5)
    with pytest.raises(ValueError, match="do not fit"):
        ticks_per_round(4, 12)


@pytest.mark.asyncio
async def test_one_round_is_one_manager_turn_then_four_worker_turns() -> None:
    manager = Replies([
        _reply('{"tool":"set_directive","arguments":{"targetRole":"all","instruction":"go"}}'),
        _reply('{"tool":null}'),
    ])
    workers = Replies([])

    result = await run_managed_case(
        _config(), manager_adapter=manager, worker_adapter=workers
    )

    assert len(manager.prompts) == 2
    assert len(workers.prompts) == 8
    assert [record.outcome for record in result.trajectory.manager_actions] == [
        "applied",
        "stood_down",
    ]
    assert result.summary["roundsPlayed"] == 2
    # Two rounds over a 24-tick horizon: each round is followed by a 12-tick
    # routine interval, so the two rounds run the whole level.
    assert result.summary["ticksPerRound"] == 12
    assert result.summary["ticks"] == 24
    assert [record.round for record in result.trajectory.manager_actions] == [1, 2]


# -- the two identities -------------------------------------------------


@pytest.mark.asyncio
async def test_manager_and_worker_identities_never_merge_in_the_record() -> None:
    manager = Replies(
        [
            _reply(
                '{"tool":"set_directive","arguments":{"targetRole":"chef","instruction":"go"}}',
                served="vendor/boss",
            )
        ],
        served="vendor/boss",
    )
    workers = Replies([], served="vendor/staff")
    sink = io.StringIO()

    result = await run_managed_case(
        _config(rounds=1),
        manager_adapter=manager,
        worker_adapter=workers,
        jsonl=sink,
    )

    summary = result.summary
    assert summary["managerModel"] == "nim:vendor/boss"
    assert summary["workerModel"] == "nim:vendor/staff"
    assert "model" not in summary
    manager_block = summary["manager"]
    workers_block = summary["workers"]
    assert isinstance(manager_block, dict) and isinstance(workers_block, dict)
    assert manager_block["servedModels"] == ["vendor/boss"]
    assert workers_block["servedModels"] == ["vendor/staff"]

    attempts = [json.loads(line) for line in sink.getvalue().splitlines()[:-1]]
    by_actor = {record["actor"] for record in attempts}
    assert by_actor == {"manager", "worker"}
    for record in attempts:
        expected = "nim:vendor/boss" if record["actor"] == "manager" else "nim:vendor/staff"
        assert record["model"] == expected


@pytest.mark.asyncio
async def test_two_managers_run_against_the_same_fixed_worker_configuration() -> None:
    grades = []
    for boss in ("nim:vendor/boss-a", "nim:vendor/boss-b"):
        served = boss.split(":", 1)[1]
        manager = Replies(
            [
                _reply(
                    '{"tool":"set_directive","arguments":{"targetRole":"chef","instruction":"go"}}',
                    served=served,
                )
            ],
            served=served,
        )
        workers = Replies([], served="vendor/staff")
        result = await run_managed_case(
            _config(manager_model=boss, rounds=1),
            manager_adapter=manager,
            worker_adapter=workers,
        )
        grades.append(result.summary)

    assert [summary["managerModel"] for summary in grades] == [
        "nim:vendor/boss-a",
        "nim:vendor/boss-b",
    ]
    # Worker identity is what is held fixed across the two manager runs.
    assert {summary["workerModel"] for summary in grades} == {"nim:vendor/staff"}
    for summary in grades:
        assert summary["runValidity"] == "valid"


# -- one instruction, followed through --------------------------------


@pytest.mark.asyncio
async def test_a_manager_directive_reaches_the_worker_it_names() -> None:
    manager = Replies([
        _reply(
            '{"tool":"set_directive","arguments":'
            + '{"targetRole":"chef","instruction":"inspect order-2 first","priority":"high"}}'
        )
    ])
    workers = Replies([])

    result = await run_managed_case(
        _config(rounds=1), manager_adapter=manager, worker_adapter=workers
    )

    # The chef's prompt carries it; the host's, which the directive does not
    # name, does not.
    chef_prompt = _user_prompt(workers.prompts[0])
    host_prompt = _user_prompt(workers.prompts[2])
    directives = chef_prompt["standingDirectives"]
    assert isinstance(directives, list)
    assert directives[0]["instruction"] == "inspect order-2 first"
    assert "standingDirectives" not in host_prompt

    # And the record says which instructions stood when each decision was made.
    digest = result.summary["finalDirectivesDigest"]
    assert result.trajectory.manager_actions[0].directives_digest == digest
    assert result.trajectory.final_directives[0].target_role == "chef"


@pytest.mark.asyncio
async def test_worker_decisions_carry_the_digest_of_the_instructions_in_force() -> None:
    # Round 1 posts nothing, round 2 posts a directive: the two rounds' worker
    # decisions must not carry the same digest, or a transcript taken under
    # one set of instructions could be replayed as though taken under another.
    manager = Replies([
        _reply('{"tool":null}'),
        _reply('{"tool":"set_directive","arguments":{"targetRole":"all","instruction":"push"}}'),
    ])
    workers = Replies(
        [_reply('{"tool":"inspect","arguments":{"targetId":"order-1"}}')] * 20
    )

    result = await run_managed_case(
        _config(), manager_adapter=manager, worker_adapter=workers
    )

    digests = {record.round: record.directives_digest for record in result.trajectory.actions}
    assert digests[1] != digests[2]
    assert all(record.round is not None for record in result.trajectory.actions)


def test_an_empty_board_leaves_the_frozen_worker_prompt_untouched() -> None:
    from retro_backend.esim.runner import build_worker_messages

    worker_ids = {"chef": "worker-chef"}
    unmanaged = build_worker_messages("chef", {"role": "chef"}, None, worker_ids=worker_ids)  # pyright: ignore[reportArgumentType]
    managed = build_worker_messages(
        "chef", {"role": "chef"}, None, worker_ids=worker_ids, directives=()  # pyright: ignore[reportArgumentType]
    )

    assert unmanaged == managed
    assert "standingDirectives" not in _user_prompt(unmanaged)


# -- the directive board ------------------------------------------------


def _manager_envelope(tool: str, arguments: Mapping[str, object]) -> ManagerActionEnvelope:
    return ManagerActionEnvelope.model_validate(
        {
            "episodeId": CASE,
            "tool": tool,
            "arguments": dict(arguments),
            "round": 1,
            "tick": 0,
            "requestId": "test-r001",
        }
    )


def test_the_board_replaces_rather_than_stacks_directives_per_target() -> None:
    board = DirectiveBoard()
    board.apply(_manager_envelope("set_directive", {"targetRole": "chef", "instruction": "one"}))
    board.apply(_manager_envelope("set_directive", {"targetRole": "chef", "instruction": "two"}))

    active = board.active()
    assert len(active) == 1
    assert active[0].instruction == "two"


def test_the_board_rejects_a_malformed_directive_and_an_empty_clear() -> None:
    board = DirectiveBoard()

    bad_args = board.apply(_manager_envelope("set_directive", {"targetRole": "sous-chef"}))
    empty_clear = board.apply(_manager_envelope("clear_directive", {"targetRole": "host"}))

    assert getattr(bad_args, "code", None) == "invalid_arguments"
    assert getattr(empty_clear, "code", None) == "invalid_state"
    assert board.active() == ()


def test_a_role_sees_its_own_directive_before_the_floor_wide_one() -> None:
    board = DirectiveBoard()
    board.apply(_manager_envelope("set_directive", {"targetRole": "all", "instruction": "floor"}))
    board.apply(_manager_envelope("set_directive", {"targetRole": "chef", "instruction": "mine"}))

    assert [d.instruction for d in board.for_role("chef")] == ["mine", "floor"]
    assert [d.instruction for d in board.for_role("server")] == ["floor"]


def test_the_digest_changes_with_the_instructions_and_not_otherwise() -> None:
    board = DirectiveBoard()
    empty = board.digest()
    board.apply(_manager_envelope("set_directive", {"targetRole": "chef", "instruction": "one"}))
    posted = board.digest()
    board.apply(_manager_envelope("clear_directive", {"targetRole": "chef"}))

    assert posted != empty
    assert board.digest() == empty


# -- what the manager may and may not see -------------------------------


def test_the_manager_projection_hides_the_schedule_and_the_scores() -> None:
    case = load_case(CASE)
    observation = observe_manager(
        case.initial_state,
        round_number=1,
        rounds_total=12,
        directives=(),
        last_round=(),
    ).model_dump(by_alias=True, mode="json")

    assert set(observation) == {
        "version",
        "role",
        "round",
        "roundsTotal",
        "tick",
        "ticksRemaining",
        "staff",
        "orders",
        "equipment",
        "tables",
        "inventory",
        "lowStock",
        "emergency",
        "standingDirectives",
        "lastRound",
    }
    # Nothing about future incidents, and nothing derived by the grader: the
    # manager sees stock on hand and a table's wait (#79), never a score.
    blob = json.dumps(observation)
    assert "eventSchedule" not in blob
    assert "outcomes" not in blob
    assert "correctionUptake" not in blob
    assert "cash" not in blob
    assert "waste" not in blob
    assert "satisfaction" not in blob


def test_the_manager_sees_the_dinner_rush_tradeoff_signals() -> None:
    """table-1's wait and fish's stock are visible from tick 0 (#79): the
    manager does not need a scripted event to notice either half of the
    tradeoff, only to keep watching as the shift runs.
    """
    case = load_case(CASE)
    observation = observe_manager(
        case.initial_state,
        round_number=1,
        rounds_total=12,
        directives=(),
        last_round=(),
    )

    table_1 = next(t for t in observation.tables if t.id == "table-1")
    assert table_1.wait_minutes == 0
    assert table_1.impatient is False

    table_2 = next(t for t in observation.tables if t.id == "table-2")
    assert table_2.has_allergy_order is True

    assert observation.inventory["fish"] == 2
    assert "fish" in observation.low_stock


def test_a_table_turns_impatient_once_its_wait_crosses_the_threshold() -> None:
    case = load_case(CASE)
    waited = case.initial_state.model_copy(
        update={
            "clock": case.initial_state.clock.model_copy(
                update={"minute": IMPATIENT_WAIT_MINUTES}
            )
        }
    )
    observation = observe_manager(
        waited, round_number=2, rounds_total=12, directives=(), last_round=()
    )

    table_1 = next(t for t in observation.tables if t.id == "table-1")
    assert table_1.impatient is True


def test_the_manager_action_envelope_is_not_a_worker_action() -> None:
    envelope = _manager_envelope("set_directive", {"targetRole": "all", "instruction": "go"})

    assert envelope.actor_type == "manager"
    # It carries no role field at all, so it cannot be handed to
    # RestaurantSim.act() by mistake -- that call would not even construct.
    assert not hasattr(envelope, "role")


def test_a_manager_reply_that_is_not_one_tool_call_is_refused() -> None:
    with pytest.raises(ValueError, match="invalid_json"):
        parse_manager_action("nope", episode_id=CASE, round_number=1, tick=0, attempt=1)
    with pytest.raises(ValueError, match="invalid_tool_call"):
        parse_manager_action(
            '{"tool":"prepare","arguments":{}}',
            episode_id=CASE,
            round_number=1,
            tick=0,
            attempt=1,
        )


# -- failure stays visible ----------------------------------------------


@pytest.mark.asyncio
async def test_a_provider_failure_marks_the_run_and_credits_no_action() -> None:
    manager = Replies([_reply(None, error=f"{ERR_TIMEOUT}: gave up")] * 3)
    workers = Replies([])

    result = await run_managed_case(
        _config(rounds=1), manager_adapter=manager, worker_adapter=workers
    )

    assert result.summary["runValidity"] == "provider_failure"
    manager_block = result.summary["manager"]
    assert isinstance(manager_block, dict)
    assert manager_block["modelErrors"] == 3
    assert manager_block["turnsLostToProvider"] == 1
    # The round is still recorded, and nothing stood in for the manager.
    recorded = result.trajectory.manager_actions
    assert len(recorded) == 1
    # "no_decision", not "stood_down": the manager never chose to post
    # nothing, its provider never answered.
    assert recorded[0].tool is None and recorded[0].outcome == "no_decision"
    assert result.trajectory.final_directives == ()


@pytest.mark.asyncio
async def test_an_error_a_retry_recovered_from_does_not_fail_the_run() -> None:
    # One timeout, then an answer. The turn happened and the model decided, so
    # the run is a result -- the attempt stays visible in modelErrors, which is
    # what a reader needs to judge how noisy the run was.
    manager = Replies(
        [
            _reply(None, error=f"{ERR_TIMEOUT}: slow"),
            _reply(
                '{"tool":"set_directive","arguments":{"targetRole":"all","instruction":"go"}}',
                served="vendor/boss",
            ),
        ],
        served="vendor/boss",
    )
    workers = Replies([], served="vendor/staff")

    result = await run_managed_case(
        _config(rounds=1), manager_adapter=manager, worker_adapter=workers
    )

    manager_block = result.summary["manager"]
    assert isinstance(manager_block, dict)
    assert manager_block["modelErrors"] == 1
    assert manager_block["turnsLostToProvider"] == 0
    assert result.summary["runValidity"] == "valid"
    assert result.trajectory.manager_actions[0].outcome == "applied"


@pytest.mark.asyncio
async def test_a_provider_that_never_names_the_model_is_not_a_valid_result() -> None:
    # Every call succeeded, so nothing failed -- but no reply said which model
    # answered, and a shape whose claim is "this manager, those workers"
    # cannot report a clean result it never confirmed.
    manager = Replies([_reply('{"tool":null}', served=None)])
    workers = Replies([_reply('{"tool":null}', served=None)] * 4)

    result = await run_managed_case(
        _config(rounds=1), manager_adapter=manager, worker_adapter=workers
    )

    assert result.summary["runValidity"] == "unverified_identity"
    manager_block = result.summary["manager"]
    workers_block = result.summary["workers"]
    assert isinstance(manager_block, dict) and isinstance(workers_block, dict)
    assert manager_block["servedModelUnverified"] is True
    assert workers_block["servedModelUnverified"] is True
    # Nothing failed, so this must not be reported as a provider failure.
    assert manager_block["modelErrors"] == 0
    assert manager_block["servedModelMismatch"] is False


@pytest.mark.asyncio
async def test_a_served_model_that_is_not_the_requested_one_is_not_valid() -> None:
    manager = Replies([_reply('{"tool":null}')], served="vendor/somebody-else")
    workers = Replies([], served="vendor/staff")

    result = await run_managed_case(
        _config(rounds=1), manager_adapter=manager, worker_adapter=workers
    )

    manager_block = result.summary["manager"]
    workers_block = result.summary["workers"]
    assert isinstance(manager_block, dict) and isinstance(workers_block, dict)
    assert manager_block["servedModelMismatch"] is True
    assert workers_block["servedModelMismatch"] is False
    assert result.summary["runValidity"] == "provider_failure"


@pytest.mark.asyncio
async def test_the_worker_budget_stops_a_run_the_manager_cannot_outlast() -> None:
    manager = Replies([_reply('{"tool":null}')] * 4)
    # Every worker reply is unusable, so each role burns its whole retry
    # allowance without producing a decision.
    workers = Replies([_reply("not json")] * 100)

    result = await run_managed_case(
        _config(rounds=2, max_attempts=1),
        manager_adapter=manager,
        worker_adapter=workers,
    )

    workers_block = result.summary["workers"]
    assert isinstance(workers_block, dict)
    assert workers_block["turns"] <= worker_turn_budget(_config(rounds=2, max_attempts=1))
    assert workers_block["invalidResponses"] == workers_block["turns"]
    assert result.summary["workers"]["acceptedActions"] == 0  # pyright: ignore[reportIndexIssue]


# -- the bundle ---------------------------------------------------------


@pytest.mark.asyncio
async def test_the_managed_bundle_records_both_models_and_the_directives(
    tmp_path: Path,
) -> None:
    manager = Replies([
        _reply('{"tool":"set_directive","arguments":{"targetRole":"chef","instruction":"go"}}')
    ])
    workers = Replies([])
    config = _config(rounds=1)
    result = await run_managed_case(
        config, manager_adapter=manager, worker_adapter=workers, jsonl=io.StringIO()
    )
    case = load_case(CASE)

    manifest = build_managed_manifest(
        config,
        case_seed=case.seed,
        contract_version=case.contract_version,
        max_ticks=case.initial_state.episode.max_ticks,
        repeat=1,
        repeats=1,
        started_at="2026-09-05T00:00:00+00:00",
    )
    write_managed_bundle(
        tmp_path, manifest=manifest, result=result, graded=grade(result.trajectory)
    )

    assert manifest["bundleVersion"] == MANAGED_BUNDLE_VERSION
    assert manifest["managerModel"] != manifest["workerModel"]
    assert manifest["budgets"]["managerRequestsPerLevel"] == 1
    assert manifest["budgets"]["workerRequestsPerLevel"] == 4
    directives = json.loads((tmp_path / "directives.json").read_text())
    assert directives["managerModel"] == "nim:vendor/boss"
    assert directives["finalDirectives"][0]["targetRole"] == "chef"
    for name in ("manifest.json", "trajectory.json", "grade.json", "summary.json"):
        assert (tmp_path / name).exists()


@pytest.mark.asyncio
async def test_a_recorded_managed_run_regrades_off_disk_without_a_model(
    tmp_path: Path,
) -> None:
    manager = Replies([
        _reply('{"tool":"set_directive","arguments":{"targetRole":"chef","instruction":"go"}}')
    ])
    workers = Replies([
        _reply('{"tool":"inspect","arguments":{"targetId":"order-1"}}')
    ] * 8)
    result = await run_managed_case(
        _config(), manager_adapter=manager, worker_adapter=workers
    )
    live = grade(result.trajectory)

    path = tmp_path / "trajectory.json"
    path.write_text(json.dumps(result.trajectory.model_dump(by_alias=True, mode="json")))
    reloaded = Trajectory.model_validate(json.loads(path.read_text()))

    assert grade(reloaded) == live
    # The manager's own record survives the round trip, which is what lets a
    # re-grade say which instructions the graded decisions were taken under.
    assert reloaded.manager_actions == result.trajectory.manager_actions
    assert reloaded.final_directives == result.trajectory.final_directives


def test_an_unmanaged_trajectory_still_loads_under_the_managed_fields() -> None:
    # Trajectories recorded before the manager shape existed carry neither
    # manager_actions nor per-action rounds; they must keep grading.
    payload = {
        "version": "restaurant-arena-trajectory/v1",
        "caseId": CASE,
        "envVersion": "restaurant-e-sim/v1",
        "contractVersion": "restaurant-arena/v1",
        "seed": 218220,
        "snapshots": [
            {
                "tick": 0,
                "state": load_case(CASE).initial_state.model_dump(by_alias=True, mode="json"),
            }
        ],
        "termination": "episode_complete",
    }

    trajectory = Trajectory.model_validate(payload)

    assert trajectory.manager_actions == ()
    assert trajectory.final_directives == ()
    assert grade(trajectory).case_id == CASE


# -- the CLI surface ----------------------------------------------------


def test_the_documented_managed_command_runs_end_to_end(tmp_path: Path) -> None:
    """The whole path the founder check runs, with stub providers in place.

    Registering a stub provider is the only way to exercise ``run-managed``
    without a key. It proves the command's plumbing -- two adapters, the round
    loop, the bundle, the overwrite guard -- and nothing about any model.
    """

    def _stub(model: str, **_: object) -> ModelAdapter:
        if model.endswith("boss"):
            return Replies(
                [
                    _reply(
                        '{"tool":"set_directive","arguments":'
                        + '{"targetRole":"chef","instruction":"verify order-2"}}',
                        served=model,
                    )
                ],
                served=model,
            )
        return Replies([], served=model)

    register_provider("stub", _stub)
    out = tmp_path / "managed"
    argv = [
        "run-managed",
        "--case",
        CASE,
        "--manager-model",
        "stub:vendor/boss",
        "--worker-model",
        "stub:vendor/staff",
        "--rounds",
        "2",
        "--output-dir",
        str(out),
    ]

    assert main(argv) == 0

    summary = json.loads((out / "summary.json").read_text())
    assert summary["managerModel"] == "stub:vendor/boss"
    assert summary["workerModel"] == "stub:vendor/staff"
    assert summary["runValidity"] == "valid"
    assert json.loads((out / "directives.json").read_text())["finalDirectives"]
    # A second run into the same directory would overwrite half the evidence.
    assert main(argv) == 3


def test_the_managed_subcommand_takes_two_models_and_defaults_to_twelve_rounds() -> None:
    args = build_parser().parse_args(
        [
            "run-managed",
            "--case",
            "contracts/esim/cases/dinner-rush-001.json",
            "--manager-model",
            "nim:vendor/boss",
            "--worker-model",
            "nim:vendor/staff",
            "--output-dir",
            "artifacts/managed",
        ]
    )

    assert args.manager_model == "nim:vendor/boss"
    assert args.worker_model == "nim:vendor/staff"
    # Unset here, so the frozen default in ManagedRunnerConfig applies rather
    # than a second copy of it living in the CLI.
    assert args.rounds is None
    assert (
        ManagedRunnerConfig(case_id=CASE, manager_model="a", worker_model="b").rounds
        == DEFAULT_ROUNDS
    )


def test_the_request_timeout_flag_reaches_the_config_and_the_manifest() -> None:
    # Unset must stay None all the way down: pinning a copy of the adapter's
    # default here is how a manifest ends up reporting a timeout the run never
    # applied.
    args = build_parser().parse_args(
        [
            "run-managed",
            "--case",
            CASE,
            "--manager-model",
            "nim:a",
            "--worker-model",
            "nim:b",
            "--output-dir",
            "artifacts/managed",
            "--request-timeout",
            "90",
        ]
    )
    assert args.request_timeout == 90.0

    config = ManagedRunnerConfig(
        case_id=CASE, manager_model="nim:a", worker_model="nim:b", request_timeout=90.0
    )
    manifest = build_managed_manifest(
        config,
        case_seed=218220,
        contract_version="restaurant-arena/v1",
        max_ticks=24,
        repeat=1,
        repeats=1,
        started_at="2026-09-05T00:00:00+00:00",
    )
    assert manifest["budgets"]["requestTimeoutSeconds"] == 90.0
    assert (
        build_managed_manifest(
            _config(),
            case_seed=218220,
            contract_version="restaurant-arena/v1",
            max_ticks=24,
            repeat=1,
            repeats=1,
            started_at="2026-09-05T00:00:00+00:00",
        )["budgets"]["requestTimeoutSeconds"]
        is None
    )

    with pytest.raises(ValueError, match="request_timeout"):
        ManagedRunnerConfig(
            case_id=CASE, manager_model="nim:a", worker_model="nim:b", request_timeout=0
        )


def test_a_standing_directive_is_capped_like_a_message() -> None:
    with pytest.raises(ValueError):
        StandingDirective(
            target_role="chef",
            instruction="x" * 257,
            issued_at_round=1,
            issued_at_tick=0,
        )
