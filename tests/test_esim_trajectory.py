"""The recorded trajectory: the grader's only input (#71)."""

import pytest
from esim_rollout import (  # pyright: ignore[reportImplicitRelativeImport]
    CASE_ID,
    greedy_rollout,
    idle_rollout,
)

from retro_backend.esim.case import ENV_VERSION, load_case
from retro_backend.esim.contracts import ActionError
from retro_backend.esim.simulator import RestaurantSim
from retro_backend.esim.trajectory import Trajectory, TrajectoryRecorder


def _recorder() -> tuple[RestaurantSim, TrajectoryRecorder]:
    case = load_case(CASE_ID)
    sim = RestaurantSim(case.initial_state, event_schedule=case.event_schedule)
    recorder = TrajectoryRecorder(
        case_id=case.case_id,
        seed=case.seed,
        env_version=ENV_VERSION,
        contract_version=case.contract_version,
        event_schedule=case.event_schedule,
    )
    return sim, recorder


class TestSnapshots:
    def test_one_snapshot_per_tick_from_zero_to_the_horizon(self):
        trajectory = idle_rollout(42)
        ticks = [snapshot.tick for snapshot in trajectory.snapshots]
        assert ticks == list(range(trajectory.initial_state.episode.max_ticks + 1))

    def test_recording_the_same_tick_twice_does_not_double_count(self):
        sim, recorder = _recorder()
        state = sim.reset(42)
        recorder.record_state(state)
        recorder.record_state(state)
        assert len(recorder.finish().snapshots) == 1

    def test_a_snapshot_is_the_state_the_tick_opened_with(self):
        # The oven fails at tick 8, so tick 8 opens with it already failed and
        # tick 7 does not. A grader reading the wrong side of that boundary
        # would blame a worker for the tick before the incident.
        trajectory = idle_rollout(42)
        before = trajectory.state_at(7)
        after = trajectory.state_at(8)
        assert before is not None and after is not None
        assert {e.id: e.status for e in before.equipment}["oven-1"] == "working"
        assert {e.id: e.status for e in after.equipment}["oven-1"] == "failed"

    def test_finishing_with_no_recorded_state_is_an_error(self):
        _, recorder = _recorder()
        with pytest.raises(ValueError, match="no recorded state"):
            recorder.finish()


class TestActions:
    def test_a_rejection_is_recorded_with_its_code(self):
        sim, recorder = _recorder()
        state = sim.reset(42)
        recorder.record_state(state)
        # A stale tick is the cheapest rejection to provoke and never mutates.
        envelope = _stale_envelope(state.episode.id)
        result = sim.act(envelope)
        assert isinstance(result, ActionError)
        recorder.record_action(envelope, result)
        record = recorder.finish().actions[0]
        assert record.accepted is False
        assert record.error_code == "stale_tick"

    def test_accepted_actions_exclude_rejections(self):
        trajectory = greedy_rollout(42)
        assert trajectory.actions
        assert all(record.accepted for record in trajectory.accepted_actions())


class TestIncidents:
    def test_the_frozen_schedule_becomes_the_event_log(self):
        trajectory = greedy_rollout(42)
        assert [(i.tick, i.kind) for i in trajectory.incidents] == [
            (8, "oven_failure"),
            (9, "player_correction"),
            (15, "warming_shelf_overheat"),
        ]

    def test_an_incident_past_the_last_tick_is_not_logged(self):
        sim, recorder = _recorder()
        recorder.record_state(sim.reset(42))
        # Nothing advanced, so only tick 0 was reached and nothing has fired.
        assert recorder.finish("turn_limit").incidents == ()

    def test_the_correction_carries_its_text_into_the_log(self):
        correction = greedy_rollout(42).incident_at("player_correction")
        assert correction is not None
        assert correction.target_id == "worker-chef"
        assert correction.text


class TestProvenance:
    def test_a_trajectory_round_trips_through_json(self):
        original = greedy_rollout(42)
        restored = Trajectory.model_validate_json(
            original.model_dump_json(by_alias=True)
        )
        assert restored == original
        assert restored.final_state_hash == original.final_state_hash

    def test_the_case_and_versions_travel_with_the_data(self):
        trajectory = greedy_rollout(42)
        assert trajectory.case_id == CASE_ID
        assert trajectory.env_version == ENV_VERSION
        assert trajectory.contract_version == "restaurant-arena/v1"
        assert trajectory.seed == 42


def _stale_envelope(episode_id: str):
    from datetime import UTC, datetime

    from retro_backend.esim.contracts import ActionEnvelope

    return ActionEnvelope(
        episode_id=episode_id,
        actor_id="worker-chef",
        actor_type="worker",
        role="chef",
        tool="inspect",
        arguments={"targetId": "order-1"},
        tick=99,
        timestamp=datetime(2026, 1, 1, tzinfo=UTC),
        request_id="stale-1",
        idempotency_key=f"{episode_id}:stale-1",
    )
