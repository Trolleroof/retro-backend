"""Golden test for bc_export.py against fixtures generated from tui-gamepigeon's
``@tui-games/trace-export`` (packages/trace-export/src/bc-export.ts), the
source of truth for BC join semantics. See retro_backend.services.bc_export
for the porting rationale (retro-backend#33).

tests/fixtures/bc_export_golden.json was produced by running
``extractBehaviorCloningSamples``/``splitForGame`` from that package against the
input chunks stored alongside each expected output, via bun. Every case records
all three option combinations the port exposes, so regenerating the fixture is
the only way to change an expectation.
"""

import json
from pathlib import Path

import pytest

from retro_backend.services.bc_export import (
    extract_behavior_cloning_samples,
    split_for_game,
)

ROOT = Path(__file__).resolve().parents[1]
GOLDEN = json.loads((ROOT / "tests/fixtures/bc_export_golden.json").read_text())
SPLITS = GOLDEN["__splits"]
CASES = {name: case for name, case in GOLDEN.items() if not name.startswith("__")}


@pytest.mark.parametrize("name", sorted(CASES))
def test_matches_typescript_reference_output(name: str) -> None:
    case = CASES[name]
    assert extract_behavior_cloning_samples(case["input"]) == case["output_default"]
    assert (
        extract_behavior_cloning_samples(case["input"], include_noops=True)
        == case["output_include_noops"]
    )
    assert (
        extract_behavior_cloning_samples(case["input"], actors=["human", "environment"])
        == case["output_actors_human_environment"]
    )


def test_ignores_non_human_actors_by_default() -> None:
    case = CASES["join_across_chunks"]
    environment_control = next(
        event
        for chunk in case["input"]
        for event in chunk["events"]
        if event["event_type"] == "control_changed" and event["payload"]["actor"] == "environment"
    )
    assert environment_control["payload"]["based_on_observation_id"] == "obs_2"
    assert all(
        sample["actor"] != "environment"
        for sample in extract_behavior_cloning_samples(case["input"])
    )
    assert any(
        sample["actor"] == "environment" for sample in case["output_actors_human_environment"]
    )


def test_game_id_ordering_follows_icu_collation_not_code_points() -> None:
    """Regression: the TS sorts game_id with ``localeCompare``, not code points.

    Sorting on the raw Python string puts ``game_A1`` first and ``game_a_b``
    second-to-last; ICU orders ``_`` < ``-`` < digits < letters with case as a
    tie-breaker only.
    """
    case = CASES["game_id_collation_order"]
    ordering = [sample["game_id"] for sample in case["output_default"]]
    assert ordering == ["game_a_b", "game_a-b", "game_a1", "game_A1", "game_ab"]
    assert ordering != sorted(ordering)
    assert [
        sample["game_id"] for sample in extract_behavior_cloning_samples(case["input"])
    ] == ordering


def test_shared_observation_id_resolves_by_collation_order() -> None:
    """Regression: event ordering is load-bearing, not cosmetic.

    ``game_a`` sorts before ``game_B`` under ICU, so ``game_B`` writes the
    observation last and is the only game whose control still resolves. Under
    code-point ordering the surviving sample would be ``game_a`` instead.
    """
    samples = extract_behavior_cloning_samples(CASES["shared_observation_id_across_games"]["input"])
    assert [sample["game_id"] for sample in samples] == ["game_B"]
    assert samples[0]["observation"]["representation"] == "repr_B"


def test_last_chunk_wins_for_multi_chunk_game_metadata() -> None:
    samples = extract_behavior_cloning_samples(CASES["multi_chunk_differing_game_type"]["input"])
    assert {sample["game_type"] for sample in samples} == {"snake"}
    assert {sample["game_version"] for sample in samples} == {"9"}


def test_split_for_game_matches_reference_hash_buckets() -> None:
    assert SPLITS["game_test"] == "train"
    assert SPLITS["game_0"] == "validation"
    assert SPLITS["game_19"] == "test"
    for game_id, expected in SPLITS.items():
        assert split_for_game(game_id) == expected
