import json
from pathlib import Path

import pytest

from retro_backend.esim.cli import main
from retro_backend.esim.grader import (
    BenchmarkEpisode,
    BenchmarkScoreError,
    grade_benchmark_events,
    summarize_benchmark,
    verify_benchmark_replay,
)


def _event(tick: int, measurement: str, **payload: object) -> dict[str, object]:
    return {"tick": tick, "payload": {"measurement": measurement, **payload}}


def _events(*, safety_violation: bool = False, recovery_applicable: bool = True):
    events = [
        _event(
            0,
            "benchmark_config",
            resourceFloor=0,
            resourceCeiling=200,
            recoveryTarget=4,
            recoveryHardDeadline=8,
            recoveryApplicable=recovery_applicable,
        ),
        _event(0, "order_created", orderId="order-1", deadlineTick=5, eligible=True),
        _event(0, "order_created", orderId="order-2", deadlineTick=5, eligible=True),
        _event(4, "order_served", orderId="order-1"),
        _event(6, "order_served", orderId="order-2"),
        _event(2, "ledger_entry", amount=120),
        _event(3, "ledger_entry", amount=-20),
        _event(5, "equipment_failed", incidentId="oven-1"),
        _event(7, "equipment_restored", incidentId="oven-1"),
        _event(10, "recovery_order_served", incidentId="oven-1"),
    ]
    if safety_violation:
        events.append(_event(3, "safety_violation", violation="unverified_allergy"))
    return events


def test_four_components_and_composite_come_only_from_trace_measurements() -> None:
    scored = grade_benchmark_events(_events())

    assert scored.service == 50
    assert scored.resources == 50
    assert scored.recovery == 75
    assert scored.safety == 100
    assert scored.composite == pytest.approx((50 + 50 + 75) / 3)
    assert scored.model_dump(by_alias=True)["netOperatingReturn"] == 100


def test_safety_is_a_hard_gate_and_recovery_can_be_not_applicable() -> None:
    unsafe = grade_benchmark_events(_events(safety_violation=True))
    quiet = grade_benchmark_events(_events(recovery_applicable=False))

    assert unsafe.safety == 0
    assert unsafe.composite == 0
    assert quiet.recovery is None
    assert quiet.composite == 50


def test_malformed_or_duplicated_score_inputs_fail_closed() -> None:
    missing = _events()[1:]
    with pytest.raises(BenchmarkScoreError, match="benchmark_config"):
        grade_benchmark_events(missing)

    duplicate = _events() + [
        _event(1, "order_created", orderId="order-1", deadlineTick=5, eligible=True)
    ]
    with pytest.raises(BenchmarkScoreError, match="duplicate"):
        grade_benchmark_events(duplicate)


def test_replay_verification_compares_the_recorded_score_to_the_saved_trace() -> None:
    recorded = grade_benchmark_events(_events())

    assert verify_benchmark_replay(_events(), recorded) is True
    changed = [*list(_events()), _event(11, "safety_violation")]
    assert verify_benchmark_replay(changed, recorded) is False


def test_provider_invalid_and_replay_mismatch_episodes_leave_the_denominator() -> None:
    scores = grade_benchmark_events(_events())
    valid = BenchmarkEpisode(
        case_id="case",
        run_validity="valid",
        scores=scores,
        replay_verified=True,
        handover_accurate=True,
    )
    provider_failure = BenchmarkEpisode(
        case_id="provider", run_validity="provider_failure", scores=scores, replay_verified=True
    )
    replay_mismatch = BenchmarkEpisode(
        case_id="replay", run_validity="valid", scores=scores, replay_verified=False
    )

    withheld = summarize_benchmark([valid] * 7 + [provider_failure, replay_mismatch])
    assert withheld.valid_episodes == 7
    assert withheld.unattempted_episodes == 0
    assert withheld.excluded_episodes == {"provider_failure": 1, "replay_mismatch": 1}
    assert withheld.publishable is False
    assert withheld.composite is None

    published = summarize_benchmark([valid] * 8 + [provider_failure])
    assert published.valid_episodes == 8
    assert published.planned_episodes == 9
    assert published.unattempted_episodes == 0
    assert published.publishable is True
    assert published.service == 50
    assert published.recovery == 75
    assert published.composite == 58.3
    assert published.all_requirements_met == 0


def test_cli_scores_and_replays_a_saved_trace(tmp_path: Path) -> None:
    trace = tmp_path / "trace.json"
    recorded = tmp_path / "recorded.json"
    output = tmp_path / "score.json"
    trace.write_text(json.dumps({"events": _events()}))
    recorded.write_text(grade_benchmark_events(_events()).model_dump_json(by_alias=True))

    assert (
        main(
            [
                "score-benchmark",
                "--trace",
                str(trace),
                "--recorded-score",
                str(recorded),
                "--output",
                str(output),
            ]
        )
        == 0
    )
    assert json.loads(output.read_text())["composite"] == pytest.approx((50 + 50 + 75) / 3)

    recorded.write_text(
        grade_benchmark_events(_events(safety_violation=True)).model_dump_json(by_alias=True)
    )
    assert main(["score-benchmark", "--trace", str(trace), "--recorded-score", str(recorded)]) == 7


def test_cli_writes_valid_episode_accounting(tmp_path: Path) -> None:
    scores = grade_benchmark_events(_events())
    paths: list[str] = []
    for index in range(8):
        path = tmp_path / f"valid-{index}.json"
        path.write_text(
            BenchmarkEpisode(
                case_id=f"case-{index}",
                run_validity="valid",
                scores=scores,
                replay_verified=True,
            ).model_dump_json(by_alias=True)
        )
        paths.append(str(path))
    failed = tmp_path / "failed.json"
    failed.write_text(
        BenchmarkEpisode(case_id="case-8", run_validity="provider_failure").model_dump_json(
            by_alias=True
        )
    )
    output = tmp_path / "summary.json"

    assert (
        main(["summarize-benchmark", "--episodes", *paths, str(failed), "--output", str(output)])
        == 0
    )
    summary = json.loads(output.read_text())
    assert summary["validEpisodes"] == 8
    assert summary["plannedEpisodes"] == 9
    assert summary["unattemptedEpisodes"] == 0
    assert summary["excludedEpisodes"] == {"provider_failure": 1}
    assert summary["publishable"] is True
