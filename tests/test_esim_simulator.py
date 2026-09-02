"""RestaurantSim (#56): determinism, validation order, and tick mechanics."""

import json
from collections.abc import Mapping
from datetime import UTC, datetime
from pathlib import Path

from retro_backend.esim.contracts import (
    ActionEnvelope,
    ActionError,
    RestaurantState,
    Role,
    Tool,
)
from retro_backend.esim.simulator import DINNER_RUSH_001_SCHEDULE, RestaurantSim
from retro_backend.esim.state_hash import state_hash

ROOT = Path(__file__).resolve().parents[1]
FIXTURE = json.loads(
    (ROOT / "contracts/fixtures/restaurant-arena-v1.json").read_text()
)


def _initial_state() -> RestaurantState:
    return RestaurantState.model_validate(FIXTURE)


def _sim() -> RestaurantSim:
    return RestaurantSim(_initial_state(), event_schedule=DINNER_RUSH_001_SCHEDULE)


def _envelope(
    *,
    actor_id: str,
    role: Role,
    tool: Tool,
    arguments: Mapping[str, object],
    tick: int,
    request_id: str = "req-1",
) -> ActionEnvelope:
    return ActionEnvelope(
        episode_id="dinner-rush-001",
        actor_id=actor_id,
        actor_type="worker",
        role=role,
        tool=tool,
        arguments=arguments,
        tick=tick,
        timestamp=datetime.now(UTC),
        request_id=request_id,
        idempotency_key=f"dinner-rush-001:{request_id}",
    )


def _prepare(order_id: str, tick: int, request_id: str = "prep"):
    return _envelope(
        actor_id="worker-chef",
        role="chef",
        tool="prepare",
        arguments={"orderId": order_id},
        tick=tick,
        request_id=request_id,
    )


def _serve(order_id: str, tick: int, verified: bool = False, request_id: str = "serve"):
    return _envelope(
        actor_id="worker-server",
        role="server",
        tool="serve",
        arguments={"orderId": order_id, "verified": verified},
        tick=tick,
        request_id=request_id,
    )


class TestReset:
    def test_starts_at_tick_zero_active(self):
        sim = _sim()
        state = sim.reset(seed=1)
        assert state.episode.tick == 0
        assert state.episode.phase == "active"
        assert state.seed == 1

    def test_same_seed_and_actions_reproduce_the_same_state_hash(self):
        def run(seed: int) -> str:
            sim = _sim()
            sim.reset(seed=seed)
            sim.act(_prepare("order-1", tick=0))
            sim.advance_time()
            sim.advance_time()
            sim.advance_time()
            result = sim.act(_serve("order-1", tick=3, verified=True))
            assert isinstance(result, RestaurantState)
            sim.advance_time()
            return state_hash(sim.checkpoint().model_dump(by_alias=True))

        assert run(42) == run(42)

    def test_different_seeds_can_diverge_prep_timing(self):
        # The seed drives prepare's ready-in-N-ticks draw; different seeds
        # are not required to diverge, but the RNG must actually be reseeded
        # by reset() rather than left running from a previous episode.
        sim = _sim()
        sim.reset(seed=1)
        first_draw = sim._rng.next()  # pyright: ignore[reportPrivateUsage]
        sim.reset(seed=1)
        second_draw = sim._rng.next()  # pyright: ignore[reportPrivateUsage]
        assert first_draw == second_draw


class TestValidationOrder:
    def test_unknown_actor_is_rejected(self):
        sim = _sim()
        sim.reset(seed=1)
        before = sim.checkpoint()
        result = sim.act(
            _envelope(
                actor_id="worker-ghost",
                role="chef",
                tool="prepare",
                arguments={"orderId": "order-1"},
                tick=0,
            )
        )
        assert isinstance(result, ActionError)
        assert result.code == "unknown_entity"
        assert sim.checkpoint() == before

    def test_role_not_permitted_is_rejected(self):
        sim = _sim()
        sim.reset(seed=1)
        before = sim.checkpoint()
        result = sim.act(
            _envelope(
                actor_id="worker-host",
                role="host",
                tool="prepare",
                arguments={"orderId": "order-1"},
                tick=0,
            )
        )
        assert isinstance(result, ActionError)
        assert result.code == "role_not_permitted"
        assert sim.checkpoint() == before

    def test_malformed_arguments_are_rejected(self):
        sim = _sim()
        sim.reset(seed=1)
        before = sim.checkpoint()
        result = sim.act(
            _envelope(
                actor_id="worker-chef",
                role="chef",
                tool="prepare",
                arguments={"orderId": "order-1", "extra": "nope"},
                tick=0,
            )
        )
        assert isinstance(result, ActionError)
        assert result.code == "invalid_arguments"
        assert sim.checkpoint() == before

    def test_unknown_entity_is_rejected(self):
        sim = _sim()
        sim.reset(seed=1)
        before = sim.checkpoint()
        result = sim.act(_prepare("order-does-not-exist", tick=0))
        assert isinstance(result, ActionError)
        assert result.code == "unknown_entity"
        assert sim.checkpoint() == before

    def test_wrong_current_state_is_rejected(self):
        sim = _sim()
        sim.reset(seed=1)
        # order-1 is "queued": not ready to be served yet.
        before = sim.checkpoint()
        result = sim.act(_serve("order-1", tick=0, verified=True))
        assert isinstance(result, ActionError)
        assert result.code == "invalid_state"
        assert sim.checkpoint() == before

    def test_stale_tick_is_rejected(self):
        sim = _sim()
        sim.reset(seed=1)
        before = sim.checkpoint()
        result = sim.act(_prepare("order-1", tick=5))
        assert isinstance(result, ActionError)
        assert result.code == "stale_tick"
        assert sim.checkpoint() == before

    def test_duplicate_idempotency_key_replays_the_original_result_once(self):
        sim = _sim()
        sim.reset(seed=1)
        first = sim.act(_prepare("order-1", tick=0, request_id="dup"))
        after_first = sim.checkpoint()
        second = sim.act(_prepare("order-1", tick=0, request_id="dup"))
        assert second == first
        assert sim.checkpoint() == after_first

    def test_episode_complete_rejects_further_actions(self):
        sim = _sim()
        sim.reset(seed=1)
        for _tick in range(sim.checkpoint().episode.max_ticks):
            sim.advance_time()
        assert sim.checkpoint().episode.phase == "complete"
        result = sim.act(_prepare("order-1", tick=sim.checkpoint().episode.tick))
        assert isinstance(result, ActionError)
        assert result.code == "episode_complete"


class TestSafety:
    def test_serving_an_unverified_allergy_order_is_unsafe(self):
        sim = _sim()
        sim.reset(seed=1)
        sim.act(_prepare("order-2", tick=0))
        for _ in range(3):
            sim.advance_time()
        state = sim.checkpoint()
        assert next(o for o in state.orders if o.id == "order-2").status == "ready"
        before = sim.checkpoint()
        result = sim.act(_serve("order-2", tick=before.episode.tick, verified=False))
        assert isinstance(result, ActionError)
        assert result.code == "unsafe_action"
        assert sim.checkpoint() == before

    def test_verified_allergy_order_may_be_served_and_counts_as_safe(self):
        sim = _sim()
        sim.reset(seed=1)
        sim.act(_prepare("order-2", tick=0))
        for _ in range(3):
            sim.advance_time()
        tick = sim.checkpoint().episode.tick
        result = sim.act(_serve("order-2", tick=tick, verified=True))
        assert isinstance(result, RestaurantState)
        served = next(o for o in result.orders if o.id == "order-2")
        assert served.status == "served"
        assert result.outcomes.safe_orders == 1


class TestScheduledIncidents:
    def test_oven_failure_lands_at_tick_eight(self):
        sim = _sim()
        sim.reset(seed=1)
        for _ in range(7):
            sim.advance_time()
        # The fixture starts with an inactive emergency record, not none.
        emergency = sim.checkpoint().emergency
        assert emergency is not None
        assert emergency.active is False
        sim.advance_time()
        state = sim.checkpoint()
        assert state.episode.tick == 8
        assert state.emergency is not None
        assert state.emergency.active
        oven = next(e for e in state.equipment if e.id == "oven-1")
        assert oven.status == "failed"

    def test_warming_shelf_probe_stays_below_its_safety_threshold(self):
        sim = _sim()
        sim.reset(seed=1)
        for _ in range(15):
            sim.advance_time()
        shelf = next(
            e for e in sim.checkpoint().equipment if e.id == "warming-shelf-1"
        )
        assert shelf.reading is not None
        assert shelf.safety_threshold is not None
        assert shelf.reading < shelf.safety_threshold
        # Non-safety: no second emergency is raised for it.
        emergency = sim.checkpoint().emergency
        assert emergency is not None
        assert emergency.kind == "oven_failure"


class TestFinalGrading:
    def test_full_episode_reaches_max_ticks_and_reports_outcomes(self):
        sim = _sim()
        sim.reset(seed=7)
        sim.act(_prepare("order-1", tick=0))
        for _ in range(sim.checkpoint().episode.max_ticks):
            sim.advance_time()
        state = sim.checkpoint()
        assert state.episode.phase == "complete"
        assert state.episode.tick == state.episode.max_ticks
        assert state.outcomes.completed >= 0
