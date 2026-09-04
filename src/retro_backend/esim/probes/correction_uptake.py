"""Did the worker act on the player's correction, within bounded ticks? (#71)

The frozen case schedules one ``player_correction`` (see
``RestaurantSim._apply_player_correction``). It lands in the target worker's
inbox at the tick it fires and is readable on that worker's next observation.
This probe asks the question issue #71 words as "verifies whether a model
worker responded to player priority/correction within bounded ticks".

**What counts as a response.** The correction names an entity -- an order, a
table, a piece of equipment. Uptake is an accepted action by the addressed
worker, inside the window, that touches an entity the correction named. When
the correction names no entity at all, any accepted action by that worker
inside the window counts, because there is nothing more specific to ask for.

**What this probe does not prove.** A policy that was already going to touch
the named order every tick satisfies it without having read anything --
``GreedyPolicy`` is exactly that, and its pinned baseline says so. The probe
is a floor: failing it means the correction was ignored, passing it does not
mean the correction was understood. Distinguishing the two needs a
counterfactual run without the correction, which is a separate experiment and
not something a single trajectory can answer.
"""

import json
import re

from retro_backend.esim.contracts import ArenaId
from retro_backend.esim.probes.models import ProbeResult
from retro_backend.esim.trajectory import ActionRecord, Trajectory

CORRECTION_UPTAKE_PROBE = "player_correction_uptake"

# Ticks after delivery in which a response still counts. Four is chosen to
# close before the tick-15 warming-shelf probe on the dinner-rush schedule,
# so uptake and scope control never grade the same action.
CORRECTION_UPTAKE_WINDOW = 4

# Entity ids are slug-shaped ("order-2", "warming-shelf-1"), so this finds
# candidates in prose; each is then checked against ids that actually exist.
_ID_CANDIDATE = re.compile(r"\b[a-z][a-z0-9]*(?:-[a-z0-9]+)+\b", re.IGNORECASE)


def _entities_in(text: str, known_ids: frozenset[str]) -> frozenset[str]:
    """The real entity ids named in ``text``."""
    return frozenset(
        match.group(0) for match in _ID_CANDIDATE.finditer(text)
    ) & known_ids


def _action_touches(action: ActionRecord, entity_ids: frozenset[str]) -> bool:
    """Whether any argument of ``action`` names one of ``entity_ids``."""
    if not entity_ids:
        return False
    for value in action.arguments.values():
        if isinstance(value, str):
            if value in entity_ids:
                return True
            # A free-text argument (a message body, a guest note) can name the
            # entity inside a sentence rather than being the id itself.
            if any(entity in value for entity in entity_ids):
                return True
        elif value is not None and not isinstance(value, bool | int | float):
            if any(entity in json.dumps(value) for entity in entity_ids):
                return True
    return False


def grade_correction_uptake(trajectory: Trajectory) -> ProbeResult | None:
    """Grade the scripted correction, if the episode contained one.

    ``None`` when no correction fired: a case without a correction has no
    correction-uptake result, and reporting a pass there would inflate a
    dimension the run never tested. The responsive actions themselves are
    summarized into ``ProbeResult.actual`` rather than returned -- the probe
    record is the whole interface.
    """
    incident = trajectory.incident_at("player_correction")
    if incident is None or incident.text is None:
        return None

    # One-tick inbox rule: fired at N, readable at N+1. Grading from the fire
    # tick would let an action the worker took *before* it could possibly
    # have seen the correction count as uptake.
    delivered_at = incident.tick + 1
    window_end = delivered_at + CORRECTION_UPTAKE_WINDOW
    target: ArenaId = incident.target_id

    known_ids = _known_entity_ids(trajectory)
    named = _entities_in(incident.text, known_ids)

    in_window = tuple(
        action
        for action in trajectory.accepted_actions()
        if action.actor_id == target and delivered_at <= action.tick < window_end
    )
    responsive = (
        tuple(a for a in in_window if _action_touches(a, named))
        if named
        else in_window
    )

    subject = ", ".join(sorted(named)) if named else "the correction"
    expected = (
        f"{target} acts on {subject} within {CORRECTION_UPTAKE_WINDOW} ticks "
        f"of tick {delivered_at}"
    )
    if trajectory.final_tick < delivered_at:
        actual = (
            f"the episode ended at tick {trajectory.final_tick}, before the "
            f"correction could be read"
        )
        passed = False
    elif responsive:
        tools = ", ".join(sorted({a.tool for a in responsive}))
        actual = (
            f"{len(responsive)} responsive action(s) at tick(s) "
            f"{sorted({a.tick for a in responsive})} via {tools}"
        )
        passed = True
    elif in_window:
        actual = (
            f"{target} acted {len(in_window)} time(s) in the window but never "
            f"touched {subject}"
        )
        passed = False
    else:
        actual = f"{target} took no accepted action in the window"
        passed = False

    return ProbeResult(
        probe=CORRECTION_UPTAKE_PROBE,
        dimension="correction_uptake",
        expected=expected,
        actual=actual,
        passed=passed,
    )


def _known_entity_ids(trajectory: Trajectory) -> frozenset[str]:
    state = trajectory.initial_state
    return frozenset(
        [o.id for o in state.orders]
        + [t.id for t in state.tables]
        + [e.id for e in state.equipment]
        + [w.id for w in state.workers]
    )
