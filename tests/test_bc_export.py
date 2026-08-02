"""Golden test for bc_export.py against fixtures generated from tui-gamepigeon's
``@tui-games/trace-export`` (packages/trace-export/src/bc-export.ts), the
source of truth for BC join semantics. See retro_backend.services.bc_export
for the porting rationale (retro-backend#33).

tests/fixtures/bc_export_golden.json was produced by running
``extractBehaviorCloningSamples`` from that package against the same input
chunks used in its own bc-export.test.ts scenarios, via bun.
"""

import json
from pathlib import Path

from retro_backend.services.bc_export import (
    extract_behavior_cloning_samples,
    split_for_game,
)

ROOT = Path(__file__).resolve().parents[1]
GOLDEN = json.loads((ROOT / "tests/fixtures/bc_export_golden.json").read_text())


def test_joins_observations_human_controls_and_outcomes_across_chunks() -> None:
    case = GOLDEN["join_across_chunks"]
    assert extract_behavior_cloning_samples(case["input"]) == case["output_default"]


def test_drops_noop_controls_by_default_and_can_retain_them_explicitly() -> None:
    case = GOLDEN["noop_default_dropped"]
    assert extract_behavior_cloning_samples(case["input"]) == case["output_default"]
    assert (
        extract_behavior_cloning_samples(case["input"], include_noops=True)
        == case["output_include_noops"]
    )


def test_ignores_non_human_actors_by_default() -> None:
    case = GOLDEN["join_across_chunks"]
    environment_control = next(
        event
        for chunk in case["input"]
        for event in chunk["events"]
        if event["event_type"] == "control_changed" and event["payload"]["actor"] == "environment"
    )
    assert environment_control["payload"]["based_on_observation_id"] == "obs_2"
    samples = extract_behavior_cloning_samples(case["input"])
    assert all(sample["actor"] != "environment" for sample in samples)

    samples_with_environment = extract_behavior_cloning_samples(
        case["input"], actors=["human", "environment"]
    )
    assert any(sample["actor"] == "environment" for sample in samples_with_environment)


def test_split_for_game_matches_reference_hash_buckets() -> None:
    assert split_for_game("game_test") == "train"
    assert split_for_game("game_0") == "validation"
    assert split_for_game("game_19") == "test"
