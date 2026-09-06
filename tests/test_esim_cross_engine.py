from retro_backend.esim.case import load_case
from retro_backend.esim.cross_engine import cross_engine_v2_report


def _report(scenario: str) -> dict[str, object]:
    case = load_case("dinner-rush-001")
    return cross_engine_v2_report(case.initial_state, case.event_schedule, scenario)  # type: ignore[arg-type]


def test_routine_report_uses_supply_lead_and_coordinates_order() -> None:
    record = _report("routine")["records"][0]
    assert record["role"] == "supply_lead"
    assert record["tool"] == "coordinate"
    assert record["tick"] == 0
    assert record["accepted"] is True


def test_incident_report_reaches_oven_failure_at_tick_eight() -> None:
    record = _report("incident")["records"][0]
    assert record["tick"] == 8
    assert record["incident"]["kind"] == "oven_failure"


def test_manager_correction_reports_canonical_directive_without_state_mutation() -> None:
    record = _report("manager-correction")["records"][0]
    assert record["accepted"] is True
    assert record["role"] == record["directive"]["targetRole"] == "supply_lead"
    assert record["v1TargetRole"] == "expediter"
    assert record["stateChanged"] is False
