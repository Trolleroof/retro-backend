"""FrozenCase loading and repeatable reset(case_id, seed) (#58)."""

import json
from pathlib import Path

import pytest

from retro_backend.esim import case as case_module
from retro_backend.esim.case import (
    CaseNotFoundError,
    CaseSchemaError,
    CaseVersionError,
    load_case,
    reset,
)
from retro_backend.esim.simulator import RestaurantSim
from retro_backend.esim.state_hash import state_hash

CASE_ID = "dinner-rush-001"


def _case_payload() -> dict[str, object]:
    cases_dir = case_module._CASES_DIR  # pyright: ignore[reportPrivateUsage]
    path = cases_dir / f"{CASE_ID}.json"
    return json.loads(path.read_text())


class TestLoadCase:
    def test_loads_the_locked_demo_case(self):
        case = load_case(CASE_ID)
        assert case.case_id == CASE_ID
        assert case.seed == 218220
        assert case.horizon == 24
        assert case.initial_state.episode.max_ticks == 24
        assert {i.kind for i in case.event_schedule} == {
            "oven_failure",
            "warming_shelf_overheat",
        }

    def test_missing_case_is_not_found(self):
        with pytest.raises(CaseNotFoundError):
            load_case("no-such-case")

    def test_path_traversal_is_rejected_as_not_found(self):
        with pytest.raises(CaseNotFoundError):
            load_case("../secrets")

    def test_env_version_mismatch_is_rejected(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ):
        payload = _case_payload()
        payload["envVersion"] = "restaurant-e-sim/v2"
        _write_case(tmp_path, monkeypatch, payload)
        with pytest.raises(CaseVersionError):
            load_case(CASE_ID)

    def test_contract_version_mismatch_is_rejected(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ):
        payload = _case_payload()
        payload["contractVersion"] = "restaurant-arena/v2"
        _write_case(tmp_path, monkeypatch, payload)
        with pytest.raises(CaseVersionError):
            load_case(CASE_ID)

    def test_schema_invalid_initial_state_is_rejected(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ):
        payload = _case_payload()
        initial_state = payload["initialState"]
        assert isinstance(initial_state, dict)
        del initial_state["episode"]
        _write_case(tmp_path, monkeypatch, payload)
        with pytest.raises(CaseSchemaError):
            load_case(CASE_ID)

    def test_malformed_json_is_rejected(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ):
        monkeypatch.setattr(case_module, "_CASES_DIR", tmp_path)
        (tmp_path / f"{CASE_ID}.json").write_text("{not json")
        with pytest.raises(CaseSchemaError):
            load_case(CASE_ID)

    def test_horizon_disagreeing_with_max_ticks_is_rejected(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ):
        payload = _case_payload()
        payload["horizon"] = 99
        _write_case(tmp_path, monkeypatch, payload)
        with pytest.raises(CaseSchemaError):
            load_case(CASE_ID)


class TestReset:
    def test_reset_is_repeatable_across_fresh_calls(self):
        sim_a = reset(CASE_ID, seed=1)
        sim_b = reset(CASE_ID, seed=1)
        assert isinstance(sim_a, RestaurantSim)
        hash_a = state_hash(sim_a.checkpoint().state.model_dump(by_alias=True))
        hash_b = state_hash(sim_b.checkpoint().state.model_dump(by_alias=True))
        assert hash_a == hash_b

    def test_reset_returns_a_playable_simulator_at_tick_zero(self):
        sim = reset(CASE_ID, seed=7)
        state = sim.checkpoint().state
        assert state.episode.tick == 0
        assert state.episode.phase == "active"
        assert state.seed == 7
        sim.advance_time()
        assert sim.checkpoint().state.episode.tick == 1


def _write_case(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, payload: dict[str, object]
) -> None:
    monkeypatch.setattr(case_module, "_CASES_DIR", tmp_path)
    (tmp_path / f"{CASE_ID}.json").write_text(json.dumps(payload))
