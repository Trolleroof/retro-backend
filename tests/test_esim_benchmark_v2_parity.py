"""``restaurant-fixed-workers/v2`` cross-engine parity, the v2 counterpart of
``test_esim_benchmark_causal_parity.py``.

v1 parity only had to agree about four operational components. v2 adds a
fixed 120-tick timer, a fixed arrival schedule, a per-level cash target and a
terminal ``targetReached`` verdict -- four more places the browser engine and
this one could quietly diverge. A divergence would mean the same model scores
differently depending on where it was graded, which makes every cross-model
comparison meaningless, so the fixtures are replayed here field for field.

``tests/fixtures/traces/benchmark-v2/*.json`` are byte-identical copies of the
files ``scripts/export-benchmark-v2-fixtures.ts`` writes in tui-gamepigeon.
Each holds one run's raw measurement events, its initial and final state, the
benchmark version, and the score that engine's own ``scoreBenchmarkEvents``
computed for exactly those events. Nothing here recomputes a formula: the
events go through :func:`grade_benchmark_events_v2` and the result is diffed
against the recorded ``tsScore``.

Three things are checked, deliberately kept apart:

1. Parity -- every score field matches, with exact equality for integers,
   booleans, ``None`` and version strings, and fixed-decimal equality for the
   0..100 scores.
2. Coverage -- the suite actually exercises both target verdicts, the ``None``
   recovery case, and a safety-zeroed run.
3. Failure behaviour -- a trace that breaks the v2 timer contract, or a
   recorded score that flatters itself, is rejected rather than scored.
"""

import json
from copy import deepcopy
from functools import cache
from pathlib import Path
from typing import Any

import pytest

from retro_backend.esim.case import FixedWorkerCaseV2, load_fixed_worker_case_v2
from retro_backend.esim.grader import (
    BenchmarkEpisodeV2,
    BenchmarkScoreError,
    BenchmarkScoresV2,
    grade_benchmark_events_v2,
    verify_benchmark_replay_v2,
)

_FIXTURES_DIR = Path(__file__).parent / "fixtures/traces/benchmark-v2"
_REGISTRY_PATH = (
    Path(__file__).resolve().parents[1] / "contracts/esim/fixed-workers-v2-case-registry.json"
)

_BENCHMARK_VERSION = "restaurant-fixed-workers/v2"

#: This engine's grader identity. It is deliberately *not* a parity field:
#: tui-gamepigeon publishes a ``benchmarkVersion`` but no grader version of
#: its own, so there is nothing on the other side to diff it against, and
#: asserting it inside the parity comparison would only be checking this
#: constant against itself. It is asserted separately below as a local
#: invariant -- the thing that would actually catch drift on this side is a
#: changed grader version against an unchanged benchmark version.
_GRADER_VERSION = "restaurant-fixed-workers-grader/v2"
_DURATION_TICKS = 120

#: Scores are compared to this many decimal places. Both engines carry IEEE
#: doubles through the same arithmetic, so the only expected difference is in
#: the last bits of a repeating division (100 * 22/24, say).
_SCORE_PLACES = 9

_CASE_IDS = [
    "quiet-tuesday-v2-target-reached",
    "quiet-tuesday-v2-target-missed",
    "dinner-rush-v2-recovery-repaired",
    "dinner-rush-v2-recovery-incomplete",
    "saturday-night-v2-capacity-bought",
    "saturday-night-v2-capacity-skipped",
    "dinner-rush-v2-timer-expiry",
    "saturday-night-v2-allergy-repeated",
]

#: Fields compared with exact equality -- integers, booleans, ``None`` and
#: version strings. A float tolerance on any of these would hide a real
#: disagreement (an off-by-one order count, a flipped target verdict).
_EXACT_FIELDS = (
    "benchmark_version",
    "duration_ticks",
    "valid_terminal_state",
    "target_reached",
    "eligible_orders",
    "on_time_orders",
    "recovery_time",
    "safety_violations",
)

#: Fields compared to ``_SCORE_PLACES``. ``recovery`` is here but is ``None``
#: on the levels with no incident, and ``None`` is matched exactly.
_SCORE_FIELDS = (
    "service",
    "resources",
    "recovery",
    "safety",
    "composite",
    "cash_earned",
    "cash_target",
    "net_operating_return",
)


def _load_fixture(case_id: str) -> dict[str, Any]:
    return json.loads((_FIXTURES_DIR / f"{case_id}.json").read_text())


@cache
def _case_for(case_id: str) -> FixedWorkerCaseV2:
    """The frozen case these fixture events are graded against.

    ``grade_benchmark_events_v2`` binds a trace to a case by the fields the
    case freezes: the arrival schedule, the cash target, the resource ceiling
    and the incident schedule. Every one of those is a property of the *level*
    -- the six cases in a level differ only by ``caseId``, ``seed`` and
    ``split`` -- so a fixture exported from a level binds equally to any case
    of that level, and the development case is chosen here because it is the
    one a reader of the registry may look at.

    The exporter names these fixtures after the behaviour they demonstrate
    (``dinner-rush-v2-timer-expiry``), not after a registry case, so the level
    comes from the fixture's own ``variationId``. If the grader ever binds on
    something seed-specific, this lookup is where it fails first.
    """
    level = _load_fixture(case_id)["variationId"]
    registry = json.loads(_REGISTRY_PATH.read_text())
    matches = [
        case
        for case in (load_fixed_worker_case_v2(name) for name in registry["developmentCases"])
        if case.level == level
    ]
    assert matches, f"no frozen development case for level {level!r}"
    return matches[0]


def _score(case_id: str) -> BenchmarkScoresV2:
    return grade_benchmark_events_v2(_load_fixture(case_id)["events"], _case_for(case_id))


def _ts_score_in_backend_shape(ts_score: dict[str, Any]) -> dict[str, Any]:
    """Flatten tui-gamepigeon's ``BenchmarkMetrics`` (nested ``raw``, camelCase)
    to this engine's flat snake_case ``BenchmarkScoresV2`` field names, so the
    two can be diffed key for key instead of by JSON shape."""
    raw = ts_score["raw"]
    return {
        "benchmark_version": ts_score["benchmarkVersion"],
        "service": ts_score["service"],
        "resources": ts_score["resources"],
        "recovery": ts_score["recovery"],
        "safety": ts_score["safety"],
        "composite": ts_score["composite"],
        "duration_ticks": ts_score["durationTicks"],
        "valid_terminal_state": ts_score["validTerminalState"],
        "cash_earned": ts_score["cashEarned"],
        "cash_target": ts_score["cashTarget"],
        "target_reached": ts_score["targetReached"],
        "eligible_orders": raw["eligibleOrders"],
        "on_time_orders": raw["onTimeOrders"],
        "net_operating_return": raw["netOperatingReturn"],
        "recovery_time": raw["recoveryTime"],
        "safety_violations": raw["safetyViolations"],
    }


def _terminal_index(events: list[dict[str, Any]]) -> int:
    return next(
        index
        for index, event in enumerate(events)
        if (event.get("payload") or {}).get("measurement") == "episode_terminal"
    )


def _with_edited_terminal(case_id: str, **edits: Any) -> list[dict[str, Any]]:
    """The fixture's events with its ``episode_terminal`` payload edited.

    A key mapped to ``None`` is deleted rather than set, so a missing field and
    a wrong field can both be expressed.
    """
    events = deepcopy(_load_fixture(case_id)["events"])
    payload = events[_terminal_index(events)]["payload"]
    for key, value in edits.items():
        if value is None:
            payload.pop(key, None)
        else:
            payload[key] = value
    return events


@pytest.mark.parametrize("case_id", _CASE_IDS)
def test_backend_grader_matches_the_typescript_engines_recorded_v2_score(case_id: str) -> None:
    fixture = _load_fixture(case_id)
    assert fixture["caseId"] == case_id
    assert fixture["benchmarkVersion"] == _BENCHMARK_VERSION

    expected = _ts_score_in_backend_shape(fixture["tsScore"])
    actual = grade_benchmark_events_v2(fixture["events"], _case_for(case_id)).model_dump()

    # Asserted here rather than through `expected`, which carries only what
    # the TypeScript fixture actually says. See `_GRADER_VERSION`.
    assert actual["grader_version"] == _GRADER_VERSION
    assert set(actual) == set(expected) | {"grader_version"}, (
        "BenchmarkScoresV2 gained or lost a field -- add it to _EXACT_FIELDS or "
        "_SCORE_FIELDS so parity still covers every score field"
    )

    mismatches: list[str] = []
    for field in _EXACT_FIELDS:
        if actual[field] != expected[field]:
            mismatches.append(f"{field}: backend {actual[field]!r} != engine {expected[field]!r}")
    for field in _SCORE_FIELDS:
        expected_value, actual_value = expected[field], actual[field]
        if expected_value is None or actual_value is None:
            if expected_value is not actual_value:
                mismatches.append(
                    f"{field}: backend {actual_value!r} != engine {expected_value!r} (N/A handling)"
                )
        elif round(actual_value, _SCORE_PLACES) != round(expected_value, _SCORE_PLACES):
            mismatches.append(f"{field}: backend {actual_value!r} != engine {expected_value!r}")

    assert not mismatches, f"{case_id}: " + "; ".join(mismatches)


@pytest.mark.parametrize("case_id", _CASE_IDS)
def test_the_v2_timer_and_cash_target_agree_with_the_fixtures_own_metadata(case_id: str) -> None:
    """The exporter writes the level's timer, arrivals and target alongside the
    events. Reading both back proves the trace is the run it claims to be, not
    just internally consistent."""
    fixture = _load_fixture(case_id)
    scores = grade_benchmark_events_v2(fixture["events"], _case_for(case_id))

    assert scores.duration_ticks == fixture["durationTicks"] == _DURATION_TICKS
    assert scores.cash_target == fixture["cashTarget"]
    assert scores.target_reached is (scores.cash_earned >= scores.cash_target)
    assert scores.valid_terminal_state is True

    events = fixture["events"]
    assert events[_terminal_index(events)]["tick"] == _DURATION_TICKS
    arrivals = [
        event["tick"]
        for event in events
        if (event.get("payload") or {}).get("measurement") == "guest_arrived"
    ]
    assert arrivals == fixture["arrivalTicks"]


def test_the_v2_fixture_suite_covers_both_verdicts_and_the_not_applicable_recovery() -> None:
    scores = [_score(case_id) for case_id in _CASE_IDS]

    assert {score.target_reached for score in scores} == {True, False}
    # Quiet Tuesday has no equipment incident, so recovery is genuinely N/A.
    # Reporting 0.0 there instead of ``None`` would silently drag the
    # composite down on this engine only.
    assert any(score.recovery is None for score in scores)
    assert any(score.recovery == 0.0 for score in scores)
    assert any(score.recovery == 100.0 for score in scores)
    assert any(score.safety == 0.0 for score in scores)


def test_backend_scorer_confirms_the_v2_recovery_and_capacity_deltas() -> None:
    """The same causal gates tui-gamepigeon asserts, scored here instead."""
    repaired = _score("dinner-rush-v2-recovery-repaired")
    incomplete = _score("dinner-rush-v2-recovery-incomplete")
    assert repaired.recovery == 100.0
    assert incomplete.recovery == 0.0
    assert repaired.target_reached is True
    assert incomplete.target_reached is False

    bought = _score("saturday-night-v2-capacity-bought")
    skipped = _score("saturday-night-v2-capacity-skipped")
    assert bought.service == skipped.service
    assert skipped.cash_earned - bought.cash_earned == pytest.approx(60.0)

    expiry = _score("dinner-rush-v2-timer-expiry")
    assert expiry.duration_ticks == _DURATION_TICKS
    assert expiry.target_reached is False
    assert expiry.cash_earned < expiry.cash_target


# -- failure behaviour -------------------------------------------------------


def test_a_trace_missing_the_v2_timer_field_is_rejected() -> None:
    with pytest.raises(BenchmarkScoreError, match="durationTicks"):
        grade_benchmark_events_v2(
            _with_edited_terminal("quiet-tuesday-v2-target-reached", durationTicks=None),
            _case_for("quiet-tuesday-v2-target-reached"),
        )


def test_a_trace_carrying_the_wrong_benchmark_version_is_rejected() -> None:
    with pytest.raises(BenchmarkScoreError, match="benchmarkVersion"):
        grade_benchmark_events_v2(
            _with_edited_terminal(
                "quiet-tuesday-v2-target-reached",
                benchmarkVersion="restaurant-fixed-workers/v1",
            ),
            _case_for("quiet-tuesday-v2-target-reached"),
        )

    events = deepcopy(_load_fixture("quiet-tuesday-v2-target-reached")["events"])
    config = next(
        event["payload"]
        for event in events
        if (event.get("payload") or {}).get("measurement") == "benchmark_config"
    )
    config["benchmarkVersion"] = "restaurant-fixed-workers/v1"
    with pytest.raises(BenchmarkScoreError, match="benchmarkVersion"):
        grade_benchmark_events_v2(events, _case_for("quiet-tuesday-v2-target-reached"))


def test_a_duplicated_event_is_rejected_rather_than_double_counted() -> None:
    fixture = _load_fixture("quiet-tuesday-v2-target-reached")

    events = deepcopy(fixture["events"])
    terminal = deepcopy(events[_terminal_index(events)])
    with pytest.raises(BenchmarkScoreError, match="episode_terminal"):
        grade_benchmark_events_v2([*events, terminal], _case_for("quiet-tuesday-v2-target-reached"))

    events = deepcopy(fixture["events"])
    index = next(
        position
        for position, event in enumerate(events)
        if (event.get("payload") or {}).get("measurement") == "guest_arrived"
    )
    arrival = deepcopy(events[index])
    with pytest.raises(BenchmarkScoreError, match="duplicate guest_arrived"):
        grade_benchmark_events_v2(
            [*events[:index], arrival, *events[index:]],
            _case_for("quiet-tuesday-v2-target-reached"),
        )


def test_a_provider_invalid_attempt_is_excluded_from_the_score() -> None:
    """A run the provider failed cannot enter a model's aggregate, however
    good the score sitting on it looks."""
    scores = _score("quiet-tuesday-v2-target-reached")
    valid = BenchmarkEpisodeV2(
        case_id="quiet-tuesday-v2-target-reached",
        run_validity="valid",
        scores=scores,
        replay_verified=True,
        handover_accurate=True,
    )
    invalid = valid.model_copy(update={"run_validity": "provider_failure"})

    assert valid.exclusion_reason is None
    assert valid.all_requirements_met is True
    assert invalid.exclusion_reason == "provider_failure"
    assert invalid.all_requirements_met is None


def test_a_model_summary_cannot_override_the_score_the_trace_supports() -> None:
    case_id = "quiet-tuesday-v2-target-missed"
    events = _load_fixture(case_id)["events"]
    honest = grade_benchmark_events_v2(events, _case_for(case_id))
    assert honest.target_reached is False
    assert verify_benchmark_replay_v2(events, honest, _case_for(case_id)) is True

    # A recorded score claiming the target was hit does not survive replay...
    flattered = honest.model_copy(
        update={"target_reached": True, "composite": 100.0, "cash_earned": honest.cash_target}
    )
    assert verify_benchmark_replay_v2(events, flattered, _case_for(case_id)) is False

    # ...and neither does rewriting the terminal summary to say so, because
    # the ledger entries, not the summary, are what the cash is read from.
    with pytest.raises(BenchmarkScoreError, match="cashEarned|targetReached"):
        grade_benchmark_events_v2(
            _with_edited_terminal(case_id, cashEarned=9_999.0, targetReached=True),
            _case_for(case_id),
        )
    episode = BenchmarkEpisodeV2(
        case_id=case_id,
        run_validity="valid",
        scores=flattered,
        replay_verified=verify_benchmark_replay_v2(events, flattered, _case_for(case_id)),
        handover_accurate=True,
    )
    assert episode.exclusion_reason == "replay_mismatch"
    assert episode.all_requirements_met is None
