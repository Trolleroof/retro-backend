"""Keep the Step 5 case list complete but deliberately unfrozen."""

import json
from collections import Counter
from pathlib import Path

REGISTRY = Path(__file__).parents[1] / "contracts/esim/held-out-case-registry.draft.json"


def test_draft_registry_has_nine_complete_cases_and_cannot_be_published_yet():
    registry = json.loads(REGISTRY.read_text())

    assert registry["status"] == "draft"
    assert registry["freezeGate"] == {
        "step3CausalGate": "must_pass",
        "step4PolicySeparationGate": "must_pass",
    }
    cases = registry["cases"]
    assert Counter(case["level"] for case in cases) == {
        "quiet-tuesday": 3,
        "dinner-rush": 3,
        "saturday-night": 3,
    }
    assert len({case["seed"] for case in cases}) == 9
    for case in cases:
        assert all(field in case for field in ("benchmarkVersion", "startingState", "eventSchedule", "thresholds", "expectedIncident"))
        assert case["benchmarkVersion"].endswith("-draft")
        assert case["expectedIncident"] in case["eventSchedule"]
