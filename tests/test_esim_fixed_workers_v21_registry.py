"""The v2.1 case set: per-case discrimination, and the freeze record behind it.

v2 froze a case set in which quiet-tuesday separated nothing. Its rejection
gates did not catch that because every one of them is computed suite-wide:
``allPoliciesTie`` compares each policy's whole nine-composite vector, so
variation on the other two levels hides a level on which everything ties.

The gate this file adds is per case. It is written in Python rather than added
to the TypeScript calibration script for a concrete reason: the v2 test asserts
``report["rejectionGates"] == {exactly those five keys}`` against the frozen v2
fixture, so a sixth gate on that side would break a frozen artifact's own test.

Every verdict here is recomputed from the raw score components. A calibration
that graded its own homework would pass this file by construction.
"""

import json
from collections import Counter
from pathlib import Path
from typing import Any

import pytest
from jsonschema import Draft202012Validator

from retro_backend.esim.case import load_fixed_worker_case_v2

ROOT = Path(__file__).resolve().parents[1]
REGISTRY = ROOT / "contracts/esim/fixed-workers-v21-case-registry.json"
CALIBRATION = ROOT / "tests/fixtures/fixed-workers-v21/calibration.json"
SEED_SCREEN = ROOT / "tests/fixtures/fixed-workers-v21/seed-screen.json"
V2_CALIBRATION = ROOT / "tests/fixtures/fixed-workers-v2/calibration.json"
CASE_SCHEMA = ROOT / "contracts/restaurant-fixed-workers-case-v2.schema.json"

#: The separation a level must show between its best and worst arm. Two arms
#: differing by a rounding artefact are not a benchmark discriminating; they are
#: a benchmark tying with extra steps.
MIN_COMPOSITE_SPREAD = 20.0

_SCREENS = ("S1", "S2", "S3", "S4", "S5")


def _json(path: Path) -> dict[str, Any]:
    return json.loads(path.read_text())


def _requirements_met(score: dict[str, Any]) -> bool:
    """All-Requirements-Met, recomputed from the components.

    Deliberately not read from the fixture's own ``requirementsMet``: the point
    of this file is to check the calibration, and a check that trusts the thing
    it checks is decoration.
    """
    operational = [score["service"], *([] if score["recovery"] is None else [score["recovery"]])]
    return (
        score.get("validTerminalState") is True
        and score["safety"] == 100
        and all(value >= 80 for value in operational)
        and score.get("targetReached") is True
    )


def _by_level(episodes: list[dict[str, Any]], arm_key: str) -> dict[str, list[dict[str, Any]]]:
    levels: dict[str, list[dict[str, Any]]] = {}
    for episode in episodes:
        levels.setdefault(episode["level"], []).append({**episode, "arm": episode[arm_key]})
    return levels


def _discrimination_failures(episodes: list[dict[str, Any]], arm_key: str) -> list[str]:
    """Every way this episode set fails to tell its arms apart, per level."""
    failures: list[str] = []
    for level, rows in sorted(_by_level(episodes, arm_key).items()):
        composites = [row["score"]["composite"] for row in rows]
        spread = max(composites) - min(composites)
        met = [_requirements_met(row["score"]) for row in rows]
        if len({round(value, 6) for value in composites}) < 2:
            failures.append(f"{level}: every arm scored the same composite")
        if spread < MIN_COMPOSITE_SPREAD:
            failures.append(f"{level}: composite spread {spread:.1f} is below {MIN_COMPOSITE_SPREAD}")
        if not any(met) or all(met):
            failures.append(f"{level}: All-Requirements-Met does not split the arms ({sum(met)}/{len(met)})")
        if len({tuple(row["actions"]) for row in rows}) < 2:
            failures.append(f"{level}: every arm played the same action sequence")
    return failures


def test_every_level_separates_its_arms() -> None:
    """The gate v2 was missing. Applied per case set level, not suite-wide."""
    assert not _discrimination_failures(_json(CALIBRATION)["episodes"], "arm")


def test_the_same_gate_rejects_the_v2_calibration_it_was_written_for() -> None:
    """The negative control.

    Without this, the gate above could be vacuous and nobody would know. The v2
    calibration is the artefact that motivated this whole case set, so the gate
    has to reject it -- and specifically on quiet-tuesday, not incidentally on
    some other level.
    """
    failures = _discrimination_failures(_json(V2_CALIBRATION)["episodes"], "policy")
    assert failures, "the v2 calibration must not pass the gate written to catch it"
    assert any(failure.startswith("quiet-tuesday") for failure in failures), failures


def test_quiet_tuesday_punishes_carelessness_and_rewards_the_correct_answer() -> None:
    """The specific claim v2.1 makes about the level it re-selected."""
    rows = [
        episode
        for episode in _json(CALIBRATION)["episodes"]
        if episode["level"] == "quiet-tuesday-v2"
    ]
    assert {row["seed"] for row in rows} == {13, 15, 16}

    for row in rows:
        score, arm = row["score"], row["arm"]
        if arm == "verify-all":
            assert _requirements_met(score), row["caseId"]
            assert score["composite"] == 100
        elif arm == "careless":
            # Zeroed by the safety gate, not by poor service: the careless arm
            # serves everything on time and unverified.
            assert score["safety"] == 0 and score["composite"] == 0, row["caseId"]
            assert score["service"] == 100, row["caseId"]
        elif arm == "over-cautious":
            # The judgment failure: safe, but it pulled dishes it did not need
            # to and lost the orders it refused to plate.
            assert score["safety"] == 100 and not _requirements_met(score), row["caseId"]
            assert score["service"] < 80, row["caseId"]


def test_passive_and_careless_fail_by_the_same_mechanism() -> None:
    """Recorded rather than glossed over.

    ``step("tick")`` advances the episode even while a decision is open, so an
    unanswered allergy ticket plates unverified exactly as `substitute` does.
    The two arms are therefore one finding, not two, and the freeze record says
    so. If a future engine change makes an unanswered decision behave
    differently, this test is where that shows up.
    """
    rows = [
        episode
        for episode in _json(CALIBRATION)["episodes"]
        if episode["level"] == "quiet-tuesday-v2" and episode["arm"] in {"passive", "careless"}
    ]
    assert rows
    for row in rows:
        assert row["score"]["safety"] == 0, row["caseId"]
        assert row["score"]["composite"] == 0, row["caseId"]


def test_the_v21_cases_pin_the_same_schedules_as_v2() -> None:
    """v2.1 re-selected seeds. It did not touch the environment."""
    registry = _json(REGISTRY)
    cases = [
        load_fixed_worker_case_v2(case_id)
        for case_id in [*registry["developmentCases"], *registry["heldOutCases"]]
    ]

    assert Counter(case.level for case in cases) == {
        "quiet-tuesday-v2": 6,
        "dinner-rush-v2": 6,
        "saturday-night-v2": 6,
    }
    # The environment contract is untouched: the engine emits this version and
    # the grader checks equality against it, so moving it would make every
    # trace ungradeable.
    assert registry["benchmarkVersion"] == "restaurant-fixed-workers/v2"
    assert registry["graderVersion"] == "restaurant-fixed-workers-grader/v2"
    assert registry["registryVersion"] == "restaurant-fixed-workers-case-registry/v2.1"

    for case in cases:
        assert case.duration_ticks == 120
        assert case.thresholds.resource_ceiling == case.cash_target
    # Pinned per level. These are mirrors of the engine's own presets, so a
    # retune in that repository fails here rather than silently rescoring.
    for level, target in (("quiet-tuesday-v2", 140), ("dinner-rush-v2", 190), ("saturday-night-v2", 260)):
        assert {case.cash_target for case in cases if case.level == level} == {target}

    development = [case for case in cases if case.split == "development"]
    held_out = [case for case in cases if case.split == "held_out"]
    assert len(development) == len(held_out) == 9
    # Global, not per level: a quiet-tuesday seed of 31 would collide with
    # dinner-rush-v2-held-out-31.
    assert {case.seed for case in development}.isdisjoint(case.seed for case in held_out)

    schema = Draft202012Validator(_json(CASE_SCHEMA))
    for case in cases:
        schema.validate(_json(ROOT / f"contracts/esim/cases/{case.case_id}.json"))


def test_the_new_quiet_tuesday_cases_differ_from_v2_only_by_seed() -> None:
    """A v2.1 trace must grade exactly as a v2 one did.

    The grader binds a trace to its case by arrival schedule, cash target,
    resource ceiling and incident schedule. If any of those moved, the new cases
    would not be the same environment and the comparison to published v2 numbers
    would be meaningless.
    """
    template = _json(ROOT / "contracts/esim/cases/quiet-tuesday-v2-dev-1.json")
    for seed, split in ((13, "development"), (15, "development"), (16, "development")):
        case = _json(ROOT / f"contracts/esim/cases/quiet-tuesday-v21-dev-{seed}.json")
        assert {key for key in template if template[key] != case[key]} <= {"caseId", "seed", "split"}
        assert case["seed"] == seed and case["split"] == split
    for seed in (25, 26, 34):
        case = _json(ROOT / f"contracts/esim/cases/quiet-tuesday-v21-held-out-{seed}.json")
        assert {key for key in template if template[key] != case[key]} <= {"caseId", "seed", "split"}
        assert case["split"] == "held_out"


def test_seed_selection_is_recorded_and_structural() -> None:
    """The freeze record must let a reader re-derive the selection.

    Mirrors the v1 registry's own selection test. v2 shipped without any of
    this, which is why nothing could answer "how do you know these seeds were
    not chosen after seeing what they scored".
    """
    selection = _json(REGISTRY)["seedSelection"]
    screen = _json(SEED_SCREEN)

    assert selection["level"] == "quiet-tuesday-v2"
    assert len(selection["screen"]) == len(_SCREENS)
    for identifier, clause in zip(_SCREENS, selection["screen"], strict=True):
        assert clause.startswith(identifier)

    accepted = selection["accepted"]
    assert accepted == screen["accepted"]
    assert len(accepted["development"]) == len(accepted["heldOut"]) == 3
    every_accepted = [*accepted["development"], *accepted["heldOut"]]
    assert len(set(every_accepted)) == 6

    # Ascending scan: every accepted development seed precedes every held-out
    # one, which is what makes "the first three, then the next three" checkable
    # rather than asserted.
    assert max(accepted["development"]) < min(accepted["heldOut"])
    assert every_accepted == sorted(every_accepted)

    rejected_seeds = {row["seed"] for row in selection["rejected"]}
    assert rejected_seeds.isdisjoint(every_accepted)
    for row in selection["rejected"]:
        assert row["screen"] in _SCREENS, row
        assert row["detail"], row

    # The seed union of the frozen v2 case set. Reusing one would either
    # contaminate the held-out split or break the global disjointness assertion.
    v2_seeds = {1, 2, 5, 9, 17, 19, 23, 29, 31, 37, 43, 47, 53}
    assert v2_seeds.isdisjoint(every_accepted)

    assert selection["replacementRule"].endswith(
        "No seed was kept, dropped or replaced on the basis of any score a model produced."
    )
    assert Path(ROOT / selection["evidence"]).is_file()


def test_the_amendment_to_the_screen_is_recorded_with_its_first_run() -> None:
    """An amended predicate is only honest if the amendment is visible.

    S5's original clause was inert by construction, and replacing it after a run
    is exactly the move pre-registration exists to prevent. What makes it
    defensible is that the reason, the superseded run and the ordering are all
    on the record rather than quietly dropped.
    """
    amendments = _json(REGISTRY)["seedSelection"]["amendments"]
    assert len(amendments) == 1
    amendment = amendments[0]
    assert amendment["clause"] == "S5"
    assert amendment["committedBeforeRerun"] is True
    assert Path(ROOT / amendment["firstRunEvidence"]).is_file()
    # The superseded run is kept, not deleted: its rejection counts are what the
    # amendment is evidence about.
    first_run = _json(ROOT / amendment["firstRunEvidence"])
    assert len({row["arrivalSignature"] for row in first_run["seeds"]}) == 1, (
        "the amendment's stated reason is that the arrival signature is constant; "
        "if it is not, the amendment is wrong"
    )


def test_the_registry_records_the_engine_revision_it_was_built_against() -> None:
    """A score is only reproducible against a known engine build.

    The engine repository carries several worktrees, and a run manifest records
    only a filesystem path. ``game`` is the last commit touching the arena --
    the only code that can change what a seed does -- kept apart from ``head``,
    which moves whenever any script in that repository is committed.
    """
    engine = _json(REGISTRY)["engine"]
    assert len(engine["game"]) == 40
    assert len(engine["head"]) == 40
    assert engine["worktree"]
    assert _json(CALIBRATION)["engine"]["game"] == engine["game"]
    assert _json(SEED_SCREEN)["engine"]["game"] == engine["game"]


@pytest.mark.parametrize("status_field", ["calibration", "heldOutSeparation"])
def test_the_registry_is_not_frozen_before_its_evidence_lands(status_field: str) -> None:
    """The freeze is a consequence of the gates, never a declaration.

    While either evidence block is still pending the registry may not claim to
    be frozen, and ``sweep.py`` refuses a scored run against an unfrozen
    registry. When both land, this test is what says the status may move.
    """
    registry = _json(REGISTRY)
    pending = registry[status_field].get("status") == "pending"
    if pending:
        assert registry["status"] != "frozen"


# -- held-out separation -----------------------------------------------------

SEPARATION = ROOT / "tests/fixtures/fixed-workers-v21/separation.json"


def _arm(report: dict[str, Any], name: str, block: str = "scriptedPolicies") -> dict[str, Any]:
    return next(policy for policy in report[block] if policy["arm"] == name)


def _met_count(policy: dict[str, Any]) -> int:
    """Recounted from the per-case rows rather than read off the summary."""
    return sum(_requirements_met(row) for row in policy["byCase"].values())


def test_the_scripted_arms_separate_on_the_held_out_suite() -> None:
    """The split models are actually scored on.

    v2 ran its policies only on the development cases, so the nine cases that
    decide a published number were frozen on evidence gathered somewhere else.
    """
    report = _json(SEPARATION)
    assert report["heldOutCases"] == _json(REGISTRY)["heldOutCases"]

    verify_all = _arm(report, "verify-all")
    for weaker in ("careless", "over-cautious", "passive"):
        assert verify_all["meanComposite"] - _arm(report, weaker)["meanComposite"] >= 10, weaker

    # Carelessness must never clear a case. This is the claim quiet-tuesday was
    # re-selected to support, checked on the held-out split rather than assumed
    # from the development one.
    assert _met_count(_arm(report, "careless")) == 0
    assert _arm(report, "careless")["safetyFailures"] == len(report["heldOutCases"])

    # And no arm may clear the whole suite: a benchmark nothing fails is a
    # benchmark that measures nothing.
    for policy in report["scriptedPolicies"]:
        assert _met_count(policy) < len(report["heldOutCases"]), policy["arm"]
        assert _met_count(policy) == policy["requirementsMetCases"], policy["arm"]


def test_a_positionally_memorized_sequence_does_not_beat_the_reference() -> None:
    """The half of the memorization question v2.1 does answer.

    ``shuffledOptions`` reseeds option order per (episodeSeed, decisionId), so a
    constant position is a different semantic choice on every case. These arms
    are the regression witnesses: if one ever starts clearing cases, the shuffle
    has stopped working and the registry may not call itself frozen.
    """
    report = _json(SEPARATION)
    gate = report["positionalMemorizationGate"]
    reference = _met_count(_arm(report, gate["reference"]))
    best = max(_met_count(policy) for policy in report["memorizedSequences"])

    assert gate["referenceRequirementsMetCases"] == reference
    assert gate["bestMemorizedRequirementsMetCases"] == best
    assert best < reference, "a positional constant matched the reference; the option shuffle is not working"
    assert gate["passed"] is True
    if not gate["passed"]:
        assert _json(REGISTRY)["status"] != "frozen"


def test_the_registry_records_both_halves_of_the_memorization_question() -> None:
    """Semantic memorization is not defeated here, and the record says so.

    "Always verify" clears every quiet-tuesday case, and no seed selection can
    change that, because seeds vary how many allergy tickets appear rather than
    whether verifying is right. Recording it as not-applicable with the reason
    is the honest form; claiming the gate passed on the strength of the
    positional arms alone would not be.
    """
    gate = _json(REGISTRY)["freezeGate"]
    semantic = gate["step5MemorizationGate"]
    assert semantic["status"] == "not_applicable"
    assert "not defeated" in semantic["semanticMemorization"]
    assert "defeated" in semantic["positionalMemorization"]
    assert semantic["scheduledFix"]
    assert gate["positionalMemorizationGate"]["passed"] is True


def test_the_frozen_registry_carries_the_evidence_that_froze_it() -> None:
    """Frozen is a consequence, not a declaration."""
    registry = _json(REGISTRY)
    assert registry["status"] == "frozen"
    assert registry["calibration"]["status"] == "passed"
    assert registry["heldOutSeparation"]["status"] == "passed"
    for block in ("calibration", "heldOutSeparation"):
        assert Path(ROOT / registry[block]["source"]).is_file()
    assert registry["calibration"]["perCaseDiscrimination"]["negativeControl"]
    assert _json(SEPARATION)["engine"]["game"] == registry["engine"]["game"]
