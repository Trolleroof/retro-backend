"""The dangerous/benign pair differs in exactly one field (#75).

The pair only isolates the allergy constraint if it differs by the allergy
constraint and nothing else. A stray edit to either file -- a changed seed, a
reworded correction, a moved incident -- would silently turn the comparison
into a measurement of two different restaurants, and nothing else in the suite
would notice. These tests are that notice.
"""

import json
from pathlib import Path

from retro_backend.esim.case import load_case
from retro_backend.esim.probes.correction_uptake import (
    _entities_in,  # pyright: ignore[reportPrivateUsage]
)

CASES = Path(__file__).resolve().parents[1] / "contracts" / "esim" / "cases"
DANGEROUS = "dinner-rush-001"
BENIGN = "dinner-rush-001-benign"


def _raw(case_id: str) -> dict[str, object]:
    return json.loads((CASES / f"{case_id}.json").read_text())


def test_pair_differs_only_by_the_allergy_and_its_ids() -> None:
    dangerous = _raw(DANGEROUS)
    benign = _raw(BENIGN)

    # Normalise the two identity fields that must differ, then require the
    # rest to be byte-equal apart from the one field under test.
    normalised = json.loads(json.dumps(benign).replace(BENIGN, DANGEROUS))
    orders_d = normalised["initialState"]["orders"]
    order_2 = next(o for o in orders_d if o["id"] == "order-2")
    assert "allergy" not in order_2
    order_2["allergy"] = "peanut"

    assert normalised == dangerous


def test_dangerous_case_carries_the_allergy_and_benign_does_not() -> None:
    def order_2(case_id: str) -> object:
        state = load_case(case_id).initial_state
        return next(o for o in state.orders if o.id == "order-2").allergy

    assert order_2(DANGEROUS) == "peanut"
    assert order_2(BENIGN) is None


def test_correction_text_is_identical_and_leaks_no_allergy() -> None:
    texts = set()
    for case_id in (DANGEROUS, BENIGN):
        incidents = [
            i for i in load_case(case_id).event_schedule if i.kind == "player_correction"
        ]
        assert len(incidents) == 1
        texts.add(incidents[0].text)

    assert len(texts) == 1, "the pair must issue the identical correction"
    text = texts.pop()
    assert text is not None
    # The benign variant has no allergy, so a correction naming one would feed
    # the model a false premise and measure credulity instead of caution.
    assert "allerg" not in text.lower()
    assert "peanut" not in text.lower()


def test_reworded_correction_still_names_the_order_the_probe_grades_on() -> None:
    """The reword is score-neutral only if the probe still finds ``order-2``."""
    incident = next(
        i for i in load_case(DANGEROUS).event_schedule if i.kind == "player_correction"
    )
    assert incident.text is not None
    known = frozenset({"order-1", "order-2", "worker-chef", "oven-1", "warming-shelf-1"})
    assert _entities_in(incident.text, known) == frozenset({"order-2"})


def test_benign_variant_cannot_produce_a_safety_violation() -> None:
    """Documents why safety must never be reported alone on the benign row.

    The violation branch requires a non-null allergy, so on this variant every
    model scores 1.0 by construction. That is a property of the case, not a
    finding about any model.
    """
    state = load_case(BENIGN).initial_state
    assert all(order.allergy is None for order in state.orders)
