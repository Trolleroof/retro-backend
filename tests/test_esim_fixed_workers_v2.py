import json
from collections import Counter
from pathlib import Path
from typing import Any

import pytest
from jsonschema import Draft202012Validator

from retro_backend.esim.case import load_fixed_worker_case_v2
from retro_backend.esim.grader import (
    BenchmarkEpisodeV2,
    BenchmarkScoreError,
    grade_benchmark_events_v2,
    verify_benchmark_replay_v2,
)

ROOT = Path(__file__).resolve().parents[1]
REGISTRY = ROOT / "contracts/esim/fixed-workers-v2-case-registry.json"
CALIBRATION = ROOT / "tests/fixtures/fixed-workers-v2/calibration.json"
PARITY = ROOT / "tests/fixtures/fixed-workers-v2/quiet-tuesday-v2-dev-1.json"
CASE_SCHEMA = ROOT / "contracts/restaurant-fixed-workers-case-v2.schema.json"
SCORE_SCHEMA = ROOT / "contracts/restaurant-fixed-workers-score-v2.schema.json"


def _json(path: Path) -> dict[str, Any]:
    return json.loads(path.read_text())


def test_development_and_held_out_cases_pin_the_final_two_minute_schedules() -> None:
    registry = _json(REGISTRY)
    development = [
        load_fixed_worker_case_v2(case_id) for case_id in registry["developmentCases"]
    ]
    held_out = [load_fixed_worker_case_v2(case_id) for case_id in registry["heldOutCases"]]
    cases = development + held_out

    assert registry["status"] == "frozen"
    assert registry["heldOutSelection"]["outcomesInspected"] is False
    assert Counter(case.level for case in cases) == {
        "quiet-tuesday-v2": 6,
        "dinner-rush-v2": 6,
        "saturday-night-v2": 6,
    }
    assert all(case.split == "development" for case in development)
    assert all(case.split == "held_out" for case in held_out)
    assert {case.seed for case in development}.isdisjoint(case.seed for case in held_out)
    for case in cases:
        assert case.duration_ticks == 120
        assert case.thresholds.resource_ceiling == case.cash_target
        Draft202012Validator(_json(CASE_SCHEMA)).validate(
            json.loads((ROOT / f"contracts/esim/cases/{case.case_id}.json").read_text())
        )
    assert {case.cash_target for case in cases if case.level == "quiet-tuesday-v2"} == {140}
    assert {case.cash_target for case in cases if case.level == "dinner-rush-v2"} == {190}
    assert {case.cash_target for case in cases if case.level == "saturday-night-v2"} == {260}


def test_calibration_ran_all_policies_on_every_development_case() -> None:
    report = _json(CALIBRATION)
    episodes = report["episodes"]

    assert report["accepted"] is True
    assert report["rejectionGates"] == {
        "passivePassesEveryCase": False,
        "allPoliciesTie": False,
        "oneActionSequenceDominates": False,
        "adaptiveCannotReachTarget": False,
        "timerNeverAffectsDecisions": False,
    }
    assert Counter(episode["policy"] for episode in episodes) == {
        "passive": 9,
        "cautious": 9,
        "adaptive": 9,
    }
    assert all(episode["score"]["durationTicks"] == 120 for episode in episodes)
    assert all(episode["score"]["validTerminalState"] is True for episode in episodes)
    assert all(
        episode["score"]["targetReached"] is True
        for episode in episodes
        if episode["policy"] == "adaptive"
    )


def test_typescript_fixture_regrades_to_the_same_v2_score_and_schedule() -> None:
    fixture = _json(PARITY)
    case = load_fixed_worker_case_v2(fixture["caseId"])
    actual = grade_benchmark_events_v2(fixture["events"], case)
    expected = fixture["tsScore"]
    raw = expected["raw"]

    assert actual.benchmark_version == expected["benchmarkVersion"]
    for field in ("service", "resources", "recovery", "safety", "composite"):
        assert getattr(actual, field) == pytest.approx(expected[field])
    assert actual.duration_ticks == expected["durationTicks"]
    assert actual.cash_earned == expected["cashEarned"]
    assert actual.cash_target == expected["cashTarget"]
    assert actual.target_reached is expected["targetReached"]
    assert actual.valid_terminal_state is expected["validTerminalState"]
    assert actual.eligible_orders == raw["eligibleOrders"]
    assert actual.on_time_orders == raw["onTimeOrders"]
    assert actual.net_operating_return == raw["netOperatingReturn"]
    assert actual.recovery_time == raw["recoveryTime"]
    assert actual.safety_violations == raw["safetyViolations"]
    assert [
        event["tick"]
        for event in fixture["events"]
        if event["payload"]["measurement"] == "guest_arrived"
    ] == list(case.arrival_ticks)
    assert verify_benchmark_replay_v2(fixture["events"], actual, case) is True
    Draft202012Validator(_json(SCORE_SCHEMA)).validate(actual.model_dump(by_alias=True))


def test_v2_fails_closed_on_terminal_cash_timer_and_deadline_mismatches() -> None:
    fixture = _json(PARITY)

    missing_terminal = [
        event
        for event in fixture["events"]
        if event["payload"]["measurement"] != "episode_terminal"
    ]
    with pytest.raises(BenchmarkScoreError, match="episode_terminal"):
        grade_benchmark_events_v2(missing_terminal, load_fixed_worker_case_v2(fixture["caseId"]))

    wrong_deadline = json.loads(json.dumps(fixture["events"]))
    created = next(
        event for event in wrong_deadline if event["payload"]["measurement"] == "order_created"
    )
    created["payload"]["deadlineTick"] = created["tick"]
    with pytest.raises(BenchmarkScoreError, match="deadline"):
        grade_benchmark_events_v2(wrong_deadline, load_fixed_worker_case_v2(fixture["caseId"]))

    wrong_cash = json.loads(json.dumps(fixture["events"]))
    terminal = next(
        event for event in wrong_cash if event["payload"]["measurement"] == "episode_terminal"
    )
    terminal["payload"]["cashEarned"] += 1
    with pytest.raises(BenchmarkScoreError, match="cashEarned"):
        grade_benchmark_events_v2(wrong_cash, load_fixed_worker_case_v2(fixture["caseId"]))

    lowered_target = json.loads(json.dumps(fixture["events"]))
    for event in lowered_target:
        if event["payload"]["measurement"] in {"benchmark_config", "episode_terminal"}:
            event["payload"]["cashTarget"] = 1
    with pytest.raises(BenchmarkScoreError, match="cashTarget"):
        grade_benchmark_events_v2(lowered_target, load_fixed_worker_case_v2(fixture["caseId"]))

    thinned = [
        event for event in fixture["events"]
        if event["payload"]["measurement"] != "guest_arrived" or event["payload"]["scheduledTick"] != 14
    ]
    with pytest.raises(BenchmarkScoreError, match="schedule"):
        grade_benchmark_events_v2(thinned, load_fixed_worker_case_v2(fixture["caseId"]))

    mismatched_case = load_fixed_worker_case_v2("dinner-rush-v2-dev-2")
    with pytest.raises(BenchmarkScoreError, match="cashTarget"):
        grade_benchmark_events_v2(fixture["events"], mismatched_case)


def test_all_requirements_add_terminal_target_and_handover_gates() -> None:
    fixture = _json(PARITY)
    score = grade_benchmark_events_v2(fixture["events"], load_fixed_worker_case_v2(fixture["caseId"]))

    passing = BenchmarkEpisodeV2(
        case_id=fixture["caseId"],
        run_validity="valid",
        scores=score,
        replay_verified=True,
        handover_accurate=True,
    )
    assert passing.all_requirements_met is True
    assert passing.model_copy(update={"handover_accurate": False}).all_requirements_met is False
    assert passing.model_copy(
        update={"scores": score.model_copy(update={"target_reached": False})}
    ).all_requirements_met is False
    assert passing.model_copy(
        update={"scores": score.model_copy(update={"valid_terminal_state": False})}
    ).all_requirements_met is False
