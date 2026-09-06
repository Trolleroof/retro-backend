"""Small v2 compatibility reports for representative Tui actions."""

import json
import sys
from collections.abc import Sequence
from datetime import UTC, datetime
from typing import Literal

from retro_backend.esim.contracts import ActionEnvelope, RestaurantState
from retro_backend.esim.manager import DirectiveBoard, ManagerActionEnvelope
from retro_backend.esim.role_normalization import normalize_role_for_v1
from retro_backend.esim.simulator import RestaurantSim, ScheduledIncident

CrossEngineScenario = Literal["routine", "incident", "manager-correction"]


def cross_engine_v2_report(
    initial_state: RestaurantState,
    event_schedule: Sequence[ScheduledIncident],
    scenario: CrossEngineScenario,
) -> dict[str, object]:
    """Run one representative v2 case and return a Tui-shaped report."""
    sim = RestaurantSim(initial_state, event_schedule)
    sim.reset(initial_state.seed)
    supply_role = "supply_lead"

    if scenario == "routine":
        envelope = ActionEnvelope(
            episode_id=initial_state.episode.id,
            actor_id="worker-expediter",
            actor_type="worker",
            role=normalize_role_for_v1(supply_role),
            tool="coordinate",
            arguments={"workerId": "worker-chef", "orderId": "order-1", "priority": "high"},
            tick=0,
            timestamp=datetime(2026, 1, 1, tzinfo=UTC),
            request_id="cross-engine-routine",
            idempotency_key="cross-engine-routine",
        )
        result = sim.act(envelope)
        return _report(scenario, {"tick": 0, "role": supply_role, "tool": "coordinate", "accepted": not hasattr(result, "code"), "state": result.model_dump(by_alias=True, mode="json") if isinstance(result, RestaurantState) else None})

    if scenario == "incident":
        target_tick = next(
            (incident.tick for incident in event_schedule if incident.kind == "oven_failure"),
            None,
        )
        if target_tick is None:
            raise ValueError("cross-engine incident scenario requires an oven_failure")
        for _ in range(target_tick):
            state = sim.advance_time()
        return _report(scenario, {"tick": state.episode.tick, "incident": state.emergency.model_dump(by_alias=True, mode="json") if state.emergency else None, "equipment": [item.model_dump(by_alias=True, mode="json") for item in state.equipment]})

    board = DirectiveBoard()
    directive = ManagerActionEnvelope(
        episode_id=initial_state.episode.id,
        tool="set_directive",
        arguments={"targetRole": normalize_role_for_v1(supply_role), "instruction": "prioritize order-1", "priority": "high"},
        round=1,
        tick=0,
        request_id="cross-engine-manager-correction",
    )
    posted = board.apply(directive)
    return _report(scenario, {"accepted": not hasattr(posted, "code"), "role": supply_role, "directive": {"targetRole": supply_role, "instruction": "prioritize order-1", "priority": "high"}, "v1TargetRole": posted.target_role if posted else None, "stateChanged": False})


def _report(scenario: CrossEngineScenario, record: dict[str, object]) -> dict[str, object]:
    return {"version": "restaurant-arena-cross-engine/v2", "scenario": scenario, "records": [record]}


def main() -> int:
    """Emit the three E-Sim reports for Tui's cross-engine checker."""
    from retro_backend.esim.case import load_case

    case = load_case("dinner-rush-001")
    reports = [
        cross_engine_v2_report(case.initial_state, case.event_schedule, scenario)
        for scenario in ("routine", "incident", "manager-correction")
    ]
    json.dump(reports, sys.stdout, sort_keys=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
