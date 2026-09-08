"""Step 3, retro-backend half: docs/restaurant-fixed-workers-defensible-benchmark-plan.md.

``tui-gamepigeon``'s ``causal-replay.test.ts`` already proves four
development instances are causal inside that engine: repairing the oven
recovers Dinner Rush (seeds 2, 5) and skipping the unneeded second stove is a
pure Resources win on Quiet Tuesday (seeds 1, 5). The plan's Step 3 finish
line is not met until this engine reproduces the same result from the exact
same events -- "retro-backend's held-out-case replay work" -- so a benchmark
score is never trusted on one engine's word alone.

``tests/fixtures/traces/benchmark-causal/*.json`` is that held-out case
registry: each fixture is one development instance's raw measurement events
(``getRecordedEvents()``) plus the score tui-gamepigeon's own
``scoreBenchmarkEvents`` computed for them, exported by
``scripts/export-benchmark-causal-fixtures.ts`` in tui-gamepigeon. These are
the same four development instances the TypeScript suite calibrates against,
not the frozen Step 5 held-out suite (not started).

Two things are checked here, deliberately kept apart:

1. Parity -- replaying each fixture's events through :func:`grade_benchmark_events`
   reproduces the score the TypeScript engine recorded for the identical events,
   field for field.
2. Causality -- the treatment-vs-no-op deltas the TypeScript suite gates on
   also hold when this engine, not that one, computes the scores.
"""

import json
from pathlib import Path
from typing import Any

import pytest

from retro_backend.esim.grader import BenchmarkScores, grade_benchmark_events

_FIXTURES_DIR = Path(__file__).parent / "fixtures/traces/benchmark-causal"

_CASE_IDS = [
    "dinner-rush-seed2-repaired",
    "dinner-rush-seed2-ignored",
    "dinner-rush-seed5-repaired",
    "dinner-rush-seed5-ignored",
    "quiet-tuesday-seed1-bought",
    "quiet-tuesday-seed1-skipped",
    "quiet-tuesday-seed5-bought",
    "quiet-tuesday-seed5-skipped",
]


def _load_fixture(case_id: str) -> dict[str, Any]:
    return json.loads((_FIXTURES_DIR / f"{case_id}.json").read_text())


def _score(case_id: str) -> BenchmarkScores:
    return grade_benchmark_events(_load_fixture(case_id)["events"])


def _ts_score_in_backend_shape(ts_score: dict[str, Any]) -> dict[str, Any]:
    """Flatten tui-gamepigeon's ``BenchmarkMetrics`` (nested ``raw``) to this
    engine's flat ``BenchmarkScores`` field names, so the two can be diffed
    key for key instead of by JSON shape."""
    raw = ts_score["raw"]
    return {
        "service": ts_score["service"],
        "resources": ts_score["resources"],
        "recovery": ts_score["recovery"],
        "safety": ts_score["safety"],
        "composite": ts_score["composite"],
        "eligible_orders": raw["eligibleOrders"],
        "on_time_orders": raw["onTimeOrders"],
        "net_operating_return": raw["netOperatingReturn"],
        "recovery_time": raw["recoveryTime"],
        "safety_violations": raw["safetyViolations"],
    }


@pytest.mark.parametrize("case_id", _CASE_IDS)
def test_backend_grader_matches_the_typescript_engines_recorded_score(case_id: str) -> None:
    fixture = _load_fixture(case_id)
    expected = _ts_score_in_backend_shape(fixture["tsScore"])
    actual = grade_benchmark_events(fixture["events"]).model_dump()

    for key, expected_value in expected.items():
        actual_value = actual[key]
        if isinstance(expected_value, float) or isinstance(actual_value, float):
            assert actual_value == pytest.approx(expected_value), f"{case_id}: {key} diverges"
        else:
            assert actual_value == expected_value, f"{case_id}: {key} diverges"


@pytest.mark.parametrize("seed", [2, 5])
def test_backend_scorer_confirms_oven_repair_recovers_dinner_rush(seed: int) -> None:
    """Same gate as tui-gamepigeon's causal-replay.test.ts, scored here."""
    repaired = _score(f"dinner-rush-seed{seed}-repaired")
    ignored = _score(f"dinner-rush-seed{seed}-ignored")

    assert ignored.recovery == 0
    assert repaired.recovery == 100
    assert (repaired.recovery - ignored.recovery) >= 10
    assert (repaired.service - ignored.service) >= 10
    assert repaired.net_operating_return != ignored.net_operating_return


@pytest.mark.parametrize("seed", [1, 5])
def test_backend_scorer_confirms_the_unneeded_stove_is_a_pure_cost_quiet_tuesday(seed: int) -> None:
    """Same gate as tui-gamepigeon's causal-replay.test.ts, scored here."""
    bought = _score(f"quiet-tuesday-seed{seed}-bought")
    skipped = _score(f"quiet-tuesday-seed{seed}-skipped")

    assert (skipped.resources - bought.resources) >= 10
    assert (skipped.net_operating_return - bought.net_operating_return) >= 60
