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

import copy
import json
from pathlib import Path
from typing import Any

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


def _join_case_input() -> list[dict[str, Any]]:
    """A mutable copy of the canonical one-sample join case."""
    return copy.deepcopy(CASES["join_across_chunks"]["input"])


def _event(chunks: list[dict[str, Any]], event_type: str, **match: object) -> dict[str, Any]:
    return next(
        event
        for chunk in chunks
        for event in chunk["events"]
        if event["event_type"] == event_type
        and all(event["payload"].get(key) == value for key, value in match.items())
    )


def test_absent_ruleset_id_reads_as_null_rather_than_discarding_the_episode() -> None:
    """Regression: nothing requires ``ruleset_id``, so it must not be fatal.

    The v2 event contract and the published JSON Schema constrain ``payload`` to
    an object and no further, and the Parquet column is OPTIONAL. Subscripting
    it raised ``KeyError``, which ``build_rows`` converts into an
    ``EpisodeConversionError`` that drops every sample in the episode.
    """
    chunks = _join_case_input()
    del _event(chunks, "episode_started")["payload"]["ruleset_id"]

    samples = extract_behavior_cloning_samples(chunks)
    assert len(samples) == len(CASES["join_across_chunks"]["output_default"])
    assert [sample["ruleset_id"] for sample in samples] == [None]


def test_observation_without_an_id_is_skipped_rather_than_fatal() -> None:
    chunks = _join_case_input()
    del _event(chunks, "observation", observation_id="obs_1")["payload"]["observation_id"]

    # obs_2 still resolves, so the one human control still joins.
    assert len(extract_behavior_cloning_samples(chunks)) == 1


def test_outcome_without_a_caused_by_id_is_skipped_rather_than_fatal() -> None:
    chunks = _join_case_input()
    del _event(chunks, "outcome_changed")["payload"]["caused_by_event_id"]

    samples = extract_behavior_cloning_samples(chunks)
    assert len(samples) == 1
    assert samples[0]["reward"] is None
    assert samples[0]["moved"] is None


def test_control_without_a_string_action_is_dropped_alone() -> None:
    """The one deliberate divergence: a sample no Parquet row can hold is skipped.

    bc-export.ts would emit it with ``action: undefined``; ``sample_to_row``
    rejects that, which would cost the whole episode instead of one control.
    """
    chunks = _join_case_input()
    del _event(chunks, "control_changed", actor="human")["payload"]["action"]

    assert extract_behavior_cloning_samples(chunks) == []


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
