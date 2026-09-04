"""The scope-control classifier, pinned against the shared two-repo corpus (#71).

``contracts/esim/probes/scope-control-cases.json`` is the contract between
this classifier and tui-gamepigeon's ``scope-probe.ts``. Both repos read the
same file and must agree on every verdict in it. If this test fails after a
change here, the TypeScript side has to change with it -- or the corpus does,
deliberately, in both repos at once.
"""

import json
from pathlib import Path
from typing import Any

import pytest

from retro_backend.esim.contracts import EquipmentItem
from retro_backend.esim.probes.scope_control import (
    classify_scope_action,
    is_benign_shelf_fault,
    summarize_scope,
)
from retro_backend.esim.trajectory import ActionRecord

ROOT = Path(__file__).resolve().parents[1]
CORPUS = json.loads(
    (ROOT / "contracts/esim/probes/scope-control-cases.json").read_text()
)


def _action(case: dict[str, Any]) -> ActionRecord:
    return ActionRecord(
        tick=case["tick"],
        role=case["role"],
        actor_id=case["actorId"],
        tool=case["tool"],
        arguments=case["arguments"],
        request_id=f"corpus-{case['tick']}-{case['tool']}",
        accepted=True,
    )


@pytest.mark.parametrize(
    "case", CORPUS["cases"], ids=[c["name"] for c in CORPUS["cases"]]
)
def test_the_shared_corpus_classifies_as_both_repos_agree(case: dict[str, Any]):
    shelf = EquipmentItem.model_validate(case.get("shelf") or CORPUS["shelf"])
    decision = classify_scope_action(
        _action(case),
        shelf=shelf,
        probe_tick=CORPUS["probeTick"],
        tick=case["tick"],
        other_faults_open=case.get("otherFaultsOpen", False),
    )
    if case["expected"] is None:
        assert decision is None
        return
    assert decision is not None
    assert decision.verdict == case["expected"]
    assert decision.false_positive is case["falsePositive"]


class TestBenignShelfFault:
    def test_a_shelf_under_its_threshold_is_benign(self):
        shelf = EquipmentItem.model_validate(CORPUS["shelf"])
        assert is_benign_shelf_fault(shelf)

    def test_a_shelf_over_its_threshold_is_not(self):
        shelf = EquipmentItem.model_validate(CORPUS["shelf"]).model_copy(
            update={"reading": 71.0}
        )
        assert not is_benign_shelf_fault(shelf)

    def test_an_oven_is_never_this_probe_s_business(self):
        oven = EquipmentItem(id="oven-1", kind="oven", status="failed")
        assert not is_benign_shelf_fault(oven)


class TestSummary:
    def test_a_probe_that_never_fired_is_pending_not_passing(self):
        assert summarize_scope(()).outcome == "pending"

    def test_one_false_positive_flips_the_whole_report(self):
        cases = CORPUS["cases"]
        scoped = next(c for c in cases if c["expected"] == "scoped_adaptation")
        halted = next(c for c in cases if c["expected"] == "over_generalized_halt")
        shelf = EquipmentItem.model_validate(CORPUS["shelf"])
        decisions = [
            d
            for c in (scoped, halted)
            if (
                d := classify_scope_action(
                    _action(c),
                    shelf=shelf,
                    probe_tick=CORPUS["probeTick"],
                    tick=c["tick"],
                )
            )
            is not None
        ]
        report = summarize_scope(
            decisions, triggered_at_tick=15, reading=68.0, safety_threshold=70.0
        )
        assert report.outcome == "over_generalized"
        assert report.false_positives == 1
        assert report.scoped_responses == 1
        assert report.below_safety_threshold
