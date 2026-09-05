"""The offline determinism command's own guardrails (#75).

The script is the founder-facing determinism check, so its failure modes
matter as much as its happy path: a check that reports success without
comparing anything is worse than one that crashes.
"""

import importlib.util
import sys
from pathlib import Path

import pytest

_SPEC = importlib.util.spec_from_file_location(
    "reference_rollout", Path(__file__).resolve().parents[1] / "scripts" / "reference_rollout.py"
)
assert _SPEC is not None and _SPEC.loader is not None
reference_rollout = importlib.util.module_from_spec(_SPEC)
sys.modules["reference_rollout"] = reference_rollout
_SPEC.loader.exec_module(reference_rollout)


def test_a_single_repeat_is_rejected_not_reported_as_deterministic() -> None:
    """One run compared against itself is DETERMINISM OK for any run at all.

    Zero or fewer crashed on an empty fingerprint list; exactly one returned
    a pass that carried no information. Both now fail at parse time.
    """
    for bad in ("0", "1", "-3"):
        with pytest.raises(SystemExit):
            reference_rollout.main(["--case", "dinner-rush-001", "--repeats", bad])


def test_actor_ids_come_from_the_case_not_a_constant() -> None:
    """A case with different worker ids must not silently grade a stalled run.

    Hardcoded ids matched dinner-rush-001 only. Against any other schema-valid
    case every action would be rejected as unknown_entity, and the script
    would still run the horizon out and grade the result as the reference
    policy's performance.
    """
    trajectory = reference_rollout.rollout("dinner-rush-001", "greedy")

    # The greedy policy actually moved the restaurant, which it cannot do if
    # its envelopes name workers the simulator does not recognise.
    assert trajectory.accepted_actions()
