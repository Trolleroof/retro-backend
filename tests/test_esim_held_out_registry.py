"""Step 5 of docs/restaurant-fixed-workers-defensible-benchmark-plan.md:
the review that stands between the held-out registry and a scored model run.

``contracts/esim/held-out-case-registry.json`` is the exact test set: nine
held-out configurations, three per level, each pinning the seed, the tick-0
state, the arrival schedule that seed produces, the incident timing, the
resource floor and ceiling, the recovery target and hard deadline, the
metrics that apply, and the benchmark version that produced them. Without a
frozen registry a model's score can move because the cases moved, which is
the one thing a benchmark may never allow.

These tests are the five checks the freeze asks for, kept executable so the
registry cannot drift out from under them:

1. every case is valid -- it loads as a :class:`FrozenCase`, and the state
   embedded in the registry is byte-for-byte the state the case file holds;
2. no case still uses the old episode-scoped allergy id;
3. every case is scored by the fixed scorer, with the constants that scorer
   actually reads;
4. no case was tuned on a model result;
5. the nine cases differ enough that one memorized action sequence cannot
   win the suite -- and if that gate ever fails again (a future case swap,
   say), the registry may not say ``frozen`` and the sweep must refuse to
   run it as a scored sweep.
"""

import hashlib
import json
from pathlib import Path
from typing import Any

import pytest

from retro_backend.esim.case import ENV_VERSION, load_case
from retro_backend.esim.contracts import ACTION_VERSION, STATE_VERSION
from retro_backend.esim.grader import BENCHMARK_GRADER_VERSION
from retro_backend.esim.sweep import SweepConfig, validate_sweep_inputs

_REPO_ROOT = Path(__file__).resolve().parents[1]
_REGISTRY_PATH = _REPO_ROOT / "contracts/esim/held-out-case-registry.json"
_SEPARATION_PATH = _REPO_ROOT / "tests/fixtures/held-out-freeze/separation.json"

_BENCHMARK_VERSION = "restaurant-fixed-workers/v1"

# The plan's case set: three instances per level, no more and no fewer.
_EXPECTED_LEVELS = {"quiet-tuesday": 3, "dinner-rush": 3, "saturday-night": 3}

# tui-gamepigeon's own SHIFT_PRESETS goal cash per level, which the engine
# emits as the benchmark_config resourceCeiling. Pinned here so a level
# retune in that repository fails this suite instead of silently rescoring
# every held-out case.
_RESOURCE_CEILING = {"quiet-tuesday": 140, "dinner-rush": 190, "saturday-night": 320}

# Every field the freeze has to publish before a model is allowed to run.
_REQUIRED_CASE_FIELDS = (
    "caseId",
    "benchmarkVersion",
    "graderVersion",
    "level",
    "seed",
    "horizon",
    "startingState",
    "orderSchedule",
    "eventSchedule",
    "expectedIncident",
    "incidentTiming",
    "thresholds",
    "applicableMetrics",
)

# Development instances (tui-gamepigeon scripts/run-step4-calibration.ts).
# A held-out case may never reuse one: they are inspected during tuning.
_DEVELOPMENT_SEEDS = {
    "quiet-tuesday": {1, 5},
    "dinner-rush": {2, 5},
    "saturday-night": {1, 2},
}


@pytest.fixture(scope="module")
def registry() -> dict[str, Any]:
    return json.loads(_REGISTRY_PATH.read_text())


@pytest.fixture(scope="module")
def cases(registry: dict[str, Any]) -> list[dict[str, Any]]:
    return registry["cases"]


@pytest.fixture(scope="module")
def separation() -> dict[str, Any]:
    return json.loads(_SEPARATION_PATH.read_text())


# ── 1. Every case is valid ──────────────────────────────────────────────


def test_the_registry_holds_three_cases_per_level(cases: list[dict[str, Any]]) -> None:
    assert len(cases) == 9
    counts: dict[str, int] = {}
    for case in cases:
        counts[case["level"]] = counts.get(case["level"], 0) + 1
    assert counts == _EXPECTED_LEVELS
    assert len({case["caseId"] for case in cases}) == 9
    assert len({(case["level"], case["seed"]) for case in cases}) == 9


def test_every_case_publishes_every_frozen_field(cases: list[dict[str, Any]]) -> None:
    for case in cases:
        missing = [field for field in _REQUIRED_CASE_FIELDS if field not in case]
        assert not missing, f"{case['caseId']} is missing {missing}"
        assert case["benchmarkVersion"] == _BENCHMARK_VERSION


def test_every_case_loads_as_a_frozen_case(cases: list[dict[str, Any]]) -> None:
    """The registry entry and the case file on disk are the same case."""
    for case in cases:
        frozen = load_case(case["caseId"])
        assert frozen.env_version == ENV_VERSION
        assert frozen.contract_version == STATE_VERSION
        assert frozen.tool_schema_version == ACTION_VERSION
        assert frozen.seed == case["seed"]
        assert frozen.horizon == case["horizon"]
        assert frozen.initial_state.episode.max_ticks == case["horizon"]
        assert frozen.initial_state.episode.id == case["caseId"]

        on_disk = json.loads((_REPO_ROOT / case["caseFile"]).read_text())
        assert on_disk["initialState"] == case["startingState"], case["caseId"]
        assert on_disk["eventSchedule"] == case["eventSchedule"], case["caseId"]

        digest = hashlib.sha256(
            json.dumps(case["startingState"], sort_keys=True, separators=(",", ":")).encode()
        ).hexdigest()
        assert case["startingStateHash"] == f"sha256:{digest}", case["caseId"]


def test_every_case_pins_its_incident_timing_and_order_schedule(
    cases: list[dict[str, Any]],
) -> None:
    for case in cases:
        schedule = case["eventSchedule"]
        assert schedule, case["caseId"]
        assert [event["tick"] for event in schedule] == sorted(event["tick"] for event in schedule)
        assert all(event["tick"] <= case["horizon"] for event in schedule)

        assert case["expectedIncident"] in schedule, case["caseId"]

        oven = [event for event in schedule if event["kind"] == "oven_failure"]
        assert bool(oven) == case["recoveryApplicable"], case["caseId"]
        if oven:
            assert case["incidentTiming"]["recoveryIncidentTick"] == oven[0]["tick"]
            assert case["expectedIncident"]["kind"] == "oven_failure"

        orders = case["orderSchedule"]["orders"]
        assert orders, case["caseId"]
        assert case["orderSchedule"]["eligibleOrders"] == sum(1 for o in orders if o["eligible"])
        for order in orders:
            assert order["deadlineTick"] > order["createdTick"], case["caseId"]
            # Eligibility is exactly "the deadline still fits in the shift";
            # anything else would score an order the shift cannot serve.
            assert order["eligible"] is (order["deadlineTick"] <= case["horizon"])


def test_the_registry_is_accepted_by_the_sweep_validator(tmp_path: Path) -> None:
    roster = tmp_path / "roster.json"
    roster.write_text(json.dumps({"models": ["nim:openai/gpt-oss-20b"]}))
    config = SweepConfig(
        benchmark_version=_BENCHMARK_VERSION,
        registry_path=_REGISTRY_PATH,
        roster_path=roster,
        output_dir=tmp_path / "out",
        dry_run=True,
    )
    _, case_ids = validate_sweep_inputs(config)
    assert len(case_ids) == 9


# ── 2. No case still uses the old allergy id ────────────────────────────


def test_no_case_uses_the_episode_scoped_allergy_id(cases: list[dict[str, Any]]) -> None:
    """`allergy_ticket` was one fixed id per episode; it is now one per order.

    A case authored against the old id would present a single allergy
    decision for a shift that raises several, and every allergy order past
    the first would be served with no decision ever offered -- an unearned
    safety failure, which the composite treats as a hard zero.
    """
    for case in cases:
        order_ids = {order["orderId"] for order in case["allergyOrders"]}
        allergy_decisions = [
            decision for decision in case["decisionIds"] if decision.startswith("allergy_ticket")
        ]
        assert "allergy_ticket" not in case["decisionIds"], case["caseId"]
        for decision in allergy_decisions:
            assert decision.startswith("allergy_ticket:"), case["caseId"]
            assert decision.removeprefix("allergy_ticket:") in order_ids, case["caseId"]
        # Every allergy order on the reference run is offered its own
        # decision, so no order can be served unverified by default.
        assert {f"allergy_ticket:{order_id}" for order_id in order_ids} <= set(allergy_decisions)


def test_the_starting_state_allergy_orders_are_declared(cases: list[dict[str, Any]]) -> None:
    declared = {case["caseId"]: {o["orderId"] for o in case["allergyOrders"]} for case in cases}
    for case in cases:
        for order in case["startingState"]["orders"]:
            if order.get("allergy"):
                assert order["id"] in declared[case["caseId"]], case["caseId"]


# ── 3. Every case uses the fixed scorer ─────────────────────────────────


def test_every_case_declares_the_fixed_scorer(
    registry: dict[str, Any], cases: list[dict[str, Any]]
) -> None:
    assert registry["graderVersion"] == BENCHMARK_GRADER_VERSION
    assert registry["scoreContract"]["graderVersion"] == BENCHMARK_GRADER_VERSION
    assert registry["scoreContract"]["scorer"].endswith("grade_benchmark_events")
    for case in cases:
        assert case["graderVersion"] == BENCHMARK_GRADER_VERSION, case["caseId"]


def test_every_case_publishes_the_constants_the_scorer_reads(cases: list[dict[str, Any]]) -> None:
    """`grade_benchmark_events` reads exactly these four; a case that
    disagreed with the engine's own benchmark_config would be scored
    against a range it was never run under."""
    for case in cases:
        thresholds = case["thresholds"]
        assert thresholds["resourceFloor"] == 0
        assert thresholds["resourceCeiling"] == _RESOURCE_CEILING[case["level"]], case["caseId"]
        assert thresholds["resourceCeiling"] > thresholds["resourceFloor"]
        if case["recoveryApplicable"]:
            assert thresholds["recoveryHardDeadline"] > thresholds["recoveryTarget"]
        else:
            assert thresholds["recoveryTarget"] is None
            assert thresholds["recoveryHardDeadline"] is None


def test_applicable_metrics_match_recovery_applicability(cases: list[dict[str, Any]]) -> None:
    for case in cases:
        expected = ["service", "resources"]
        if case["recoveryApplicable"]:
            expected.append("recovery")
        expected.append("safety")
        assert case["applicableMetrics"] == expected, case["caseId"]
        # Recovery is N/A on Quiet Tuesday and must not be silently scored 0.
        assert (case["level"] == "quiet-tuesday") is not case["recoveryApplicable"]


# ── 4. No case was tuned on a model result ──────────────────────────────


def test_held_out_seeds_are_disjoint_from_the_development_instances(
    cases: list[dict[str, Any]],
) -> None:
    for case in cases:
        assert case["seed"] not in _DEVELOPMENT_SEEDS[case["level"]], case["caseId"]


def test_seed_selection_is_recorded_and_structural(registry: dict[str, Any]) -> None:
    """A replacement is only defensible when its reason is a property of the
    case, not of anybody's score."""
    selection = registry["seedSelection"]
    assert selection["candidates"].startswith("the nine seeds pre-registered")
    assert len(selection["screen"]) == 3
    held_out_seeds = {case["seed"] for case in registry["cases"]}
    for replacement in selection["replacements"]:
        assert replacement["screen"] in {"R1", "R2", "R3"}
        assert replacement["accepted"] in held_out_seeds
        assert replacement["preRegistered"] != replacement["accepted"]
        for rejection in replacement["rejected"]:
            # Every rejection names the structural screen it failed. A
            # replacement without one would be indistinguishable from a seed
            # dropped because its scores were inconvenient.
            assert rejection["screen"] in {"R1", "R2", "R3"}
            assert rejection["detail"]
            assert rejection["seed"] not in held_out_seeds


def test_no_model_identity_appears_anywhere_in_the_registry() -> None:
    """The registry defines the test, so it must be readable without knowing
    which model scored what. Any model name in here is evidence the cases
    were shaped around a result."""
    text = _REGISTRY_PATH.read_text().lower()
    for marker in ("gpt-", "claude", "llama", "qwen", "mistral", "nim:", "leaderboard"):
        assert marker not in text, f"{marker!r} appears in the registry"


def test_the_separation_evidence_is_scripted_not_model_derived(separation: dict[str, Any]) -> None:
    assert "no model was called" in separation["generatedFrom"]
    assert separation["graderVersion"] == BENCHMARK_GRADER_VERSION


# ── 5. One memorized sequence must not win the suite ────────────────────


def test_the_nine_cases_have_distinct_arrival_schedules(cases: list[dict[str, Any]]) -> None:
    """Two cases with the same schedule are one case run twice, and reward
    the same replayed sequence twice."""
    schedules = {
        case["caseId"]: tuple(
            (order["createdTick"], order["deadlineTick"])
            for order in case["orderSchedule"]["orders"]
            if order["eligible"]
        )
        for case in cases
    }
    assert len(set(schedules.values())) == len(schedules), schedules


def test_the_levels_do_not_all_measure_the_same_thing(cases: list[dict[str, Any]]) -> None:
    by_level: dict[str, list[dict[str, Any]]] = {}
    for case in cases:
        by_level.setdefault(case["level"], []).append(case)
    # Recovery applies on two levels and not the third, and the resource
    # ranges differ, so a single policy cannot be tuned to one shape.
    assert {case["recoveryApplicable"] for case in cases} == {True, False}
    assert len({case["thresholds"]["resourceCeiling"] for case in cases}) == 3
    assert len({len(case["orderSchedule"]["orders"]) for case in cases}) > 1
    assert {len(group) for group in by_level.values()} == {3}


def test_the_scripted_policies_still_separate_on_the_held_out_suite(
    separation: dict[str, Any],
) -> None:
    """Step 4's gate, re-checked on the held-out nine rather than on the
    development six: the reference policy has to be worth something."""
    arms = {arm["arm"]: arm for arm in separation["scriptedPolicies"]}
    adaptive = arms["adaptive"]
    for weaker in ("passive", "cautious"):
        assert adaptive["meanComposite"] - arms[weaker]["meanComposite"] >= 10, weaker
    # Quiet Tuesday's whole purpose: intervening for its own sake must cost.
    assert arms["passive-spender"]["meanResources"] < arms["passive"]["meanResources"]
    assert arms["passive-spender"]["requirementsMetCases"] < 9
    # And no scripted policy sweeps the suite.
    for arm in arms.values():
        assert arm["requirementsMetCases"] < 9, arm["arm"]


def _requirements_met(score: dict[str, Any]) -> bool:
    """The same rule `BenchmarkEpisode.all_requirements_met` applies:
    Safety 100 and every applicable operational component at least 80.
    Recomputed here from the raw per-case numbers rather than trusted from
    any arm's own `requirementsMet`/`requirementsMetCases` field, which are
    exactly the fields a stale or hand-edited fixture would get wrong."""
    operational = [score["service"], score["resources"]]
    if score["recovery"] is not None:
        operational.append(score["recovery"])
    return score["safety"] == 100 and all(value >= 80 for value in operational)


def _independent_requirements_met_count(arm: dict[str, Any]) -> int:
    """Recount one arm's All-Requirements-Met cases straight from
    `byCase`, ignoring the arm's own precomputed `requirementsMetCases`
    and per-case `requirementsMet` (both cross-checked below, not relied
    on)."""
    count = 0
    for case_id, score in arm["byCase"].items():
        met = _requirements_met(score)
        assert score["requirementsMet"] == met, (
            f"{arm['arm']}/{case_id}: byCase.requirementsMet disagrees with its own raw scores"
        )
        count += met
    return count


def test_a_memorized_sequence_must_not_beat_the_adaptive_reference(
    separation: dict[str, Any], registry: dict[str, Any]
) -> None:
    """The freeze gate. A fixed sequence that never reads the observation
    should not out-score a policy that responds to the shift.

    Everything this test asserts is derived from `byCase` raw scores, not
    from `memorizationGate`'s own summary fields (`passed`,
    `bestMemorizedSequence`, `*RequirementsMetCases`) or from any arm's
    `requirementsMetCases` -- those are cross-checked against the
    recomputed values at the end, not taken as the source of truth. A
    fixture whose summary and per-case data disagreed would fail here even
    if `memorizationGate.passed` said the freeze was clean.

    tui-gamepigeon used to list the scoring-optimal option first in
    ``allergy_ticket`` (verify), ``oven_down`` (repair) and ``rush_wave``
    (seat_all), so "always take the first option" cleared the suite without
    looking at anything -- fixed by seeding a per-episode option shuffle
    (docs/esim-held-out-freeze.md). Until a fix like that lands, this test
    holds the consequence: the registry may not call itself frozen, and the
    sweep must refuse to run it as a scored sweep.
    """
    policies = {arm["arm"]: arm for arm in separation["scriptedPolicies"]}
    memorized = {arm["arm"]: arm for arm in separation["memorizedSequences"]}
    assert "adaptive" in policies
    assert memorized, "no memorized sequences to screen against"

    adaptive_count = _independent_requirements_met_count(policies["adaptive"])
    memorized_counts = {
        name: _independent_requirements_met_count(arm) for name, arm in memorized.items()
    }
    best_name, best_count = max(memorized_counts.items(), key=lambda item: item[1])
    independently_passed = best_count <= adaptive_count

    gate = separation["memorizationGate"]
    recorded = registry["freezeGate"]["step5MemorizationGate"]

    # The fixture's own summary must agree with what byCase actually shows.
    assert adaptive_count == gate["adaptiveReferenceRequirementsMetCases"]
    assert best_count == gate["bestMemorizedRequirementsMetCases"]
    assert gate["passed"] == independently_passed
    assert recorded["status"] == ("passed" if independently_passed else "failed")

    if independently_passed:
        assert registry["status"] == "frozen"
        return

    assert best_count > adaptive_count, best_name
    assert registry["status"] != "frozen"
    assert "option order" in recorded["detail"]


def test_a_frozen_registry_is_accepted_for_a_real_non_dry_run_sweep(tmp_path: Path) -> None:
    """The mirror of the guard above: once every gate passes and the
    registry says ``frozen``, `validate_sweep_inputs` must stop refusing it.
    A freeze that a real sweep still can't use would not be a freeze."""
    roster = tmp_path / "roster.json"
    roster.write_text(json.dumps({"models": ["nim:openai/gpt-oss-20b"]}))
    config = SweepConfig(
        benchmark_version=_BENCHMARK_VERSION,
        registry_path=_REGISTRY_PATH,
        roster_path=roster,
        output_dir=tmp_path / "out",
        dry_run=False,
    )
    registry = json.loads(_REGISTRY_PATH.read_text())
    assert registry["status"] == "frozen", "the registry must pass its own freeze gate"
    _, case_ids = validate_sweep_inputs(config)
    assert len(case_ids) == 9


def test_an_unfrozen_copy_of_the_registry_is_refused_by_a_real_sweep(
    tmp_path: Path, registry: dict[str, Any]
) -> None:
    """The guard itself, exercised directly rather than by waiting for a
    future regression to unfreeze the real file."""
    unfrozen = tmp_path / "registry.json"
    unfrozen.write_text(json.dumps({**registry, "status": "pending-freeze"}))
    roster = tmp_path / "roster.json"
    roster.write_text(json.dumps({"models": ["nim:openai/gpt-oss-20b"]}))
    config = SweepConfig(
        benchmark_version=_BENCHMARK_VERSION,
        registry_path=unfrozen,
        roster_path=roster,
        output_dir=tmp_path / "out",
        dry_run=False,
    )
    with pytest.raises(ValueError, match="not frozen"):
        validate_sweep_inputs(config)
