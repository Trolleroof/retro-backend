"""Reference policies (#56): determinism, validity, and a greedy-beats-idle floor.

These drive a real ``RestaurantSim`` rather than a fake, because the whole
point of the policies is that they respect the observation projection and the
action validator. A policy that could only satisfy a stub would prove nothing.

The rollout deliberately reads state only from what the sim hands back
(``reset``, ``act``, ``advance_time``) so it does not depend on the checkpoint
shape, which is being reworked in parallel.
"""

import json
from pathlib import Path

from retro_backend.esim.contracts import (
    ActionEnvelope,
    ActionError,
    AvailableTable,
    HostObservation,
    QueuedParty,
    RestaurantState,
    Role,
)
from retro_backend.esim.policies import (
    POLICY_EPOCH,
    GreedyPolicy,
    IdlePolicy,
    Policy,
    idempotency_key_for,
    request_id_for,
)
from retro_backend.esim.rng import Rng
from retro_backend.esim.simulator import DINNER_RUSH_001_SCHEDULE, RestaurantSim
from retro_backend.esim.state_hash import state_hash

ROOT = Path(__file__).resolve().parents[1]
FIXTURE = json.loads(
    (ROOT / "contracts/fixtures/restaurant-arena-v1.json").read_text()
)

EPISODE_ID = "dinner-rush-001"
# Fixed order so a multi-role tick is itself deterministic.
ROLES: tuple[Role, ...] = ("chef", "server", "host", "expediter")
ACTOR_IDS: dict[Role, str] = {
    "chef": "worker-chef",
    "server": "worker-server",
    "host": "worker-host",
    "expediter": "worker-expediter",
}


def _initial_state() -> RestaurantState:
    return RestaurantState.model_validate(FIXTURE)


def _sim() -> RestaurantSim:
    return RestaurantSim(_initial_state(), event_schedule=DINNER_RUSH_001_SCHEDULE)


def _greedy(role: Role, rng: Rng | None = None) -> GreedyPolicy:
    return GreedyPolicy(
        episode_id=EPISODE_ID, actor_id=ACTOR_IDS[role], role=role, rng=rng
    )


def _idle(role: Role) -> IdlePolicy:
    return IdlePolicy(episode_id=EPISODE_ID, actor_id=ACTOR_IDS[role], role=role)


class Rollout:
    """One finished episode: what was emitted, and where the state landed."""

    def __init__(
        self, envelopes: list[ActionEnvelope], final_state: RestaurantState
    ) -> None:
        self.envelopes = envelopes
        self.final_state = final_state

    @property
    def wire(self) -> list[str]:
        """The exact bytes each envelope would go on the wire as."""
        return [e.model_dump_json(by_alias=True) for e in self.envelopes]

    @property
    def final_hash(self) -> str:
        return state_hash(self.final_state.model_dump(by_alias=True))


def run_rollout(
    policies: dict[Role, Policy], seed: int, *, sim: RestaurantSim | None = None
) -> Rollout:
    """Drive ``sim`` to episode completion with one policy per role."""
    sim = sim or _sim()
    state = sim.reset(seed=seed)
    for policy in policies.values():
        policy.reset()
    emitted: list[ActionEnvelope] = []

    while state.episode.phase == "active":
        for role in ROLES:
            policy = policies.get(role)
            if policy is None:
                continue
            envelope = policy.decide(sim.observe(role))
            if envelope is None:
                continue
            result = sim.act(envelope)
            # Load-bearing: a reference policy must never emit something the
            # validator rejects. A rejection here is a policy bug, not a
            # simulator bug.
            assert not isinstance(result, ActionError), (
                f"{role} policy emitted a rejected action: "
                f"{envelope.tool} {dict(envelope.arguments)} -> "
                f"{result.code}: {result.message}"
            )
            emitted.append(envelope)
            state = result
        state = sim.advance_time()

    return Rollout(emitted, state)


class TestEnvelopeIdentity:
    def test_idempotency_key_is_episode_tick_and_action(self):
        policy = _greedy("chef")
        first = policy.decide(_sim_at_tick_zero().observe("chef"))
        assert first is not None
        assert first.request_id == request_id_for("greedy", "chef", 0, "prepare")
        assert first.idempotency_key == idempotency_key_for(
            EPISODE_ID, first.request_id
        )
        assert first.idempotency_key == "dinner-rush-001:greedy-chef-t0000-prepare"

    def test_timestamp_is_derived_from_the_tick_not_the_wall_clock(self):
        sim = _sim_at_tick_zero()
        one = _greedy("chef").decide(sim.observe("chef"))
        two = _greedy("chef").decide(sim.observe("chef"))
        assert one is not None and two is not None
        assert one.timestamp == two.timestamp == POLICY_EPOCH

    def test_the_same_observation_yields_byte_identical_envelopes(self):
        sim = _sim_at_tick_zero()
        one = _greedy("chef").decide(sim.observe("chef"))
        two = _greedy("chef").decide(sim.observe("chef"))
        assert one is not None and two is not None
        assert one.model_dump_json(by_alias=True) == two.model_dump_json(by_alias=True)


def _sim_at_tick_zero() -> RestaurantSim:
    sim = _sim()
    sim.reset(seed=42)
    return sim


class TestGreedyRollout:
    def test_replay_from_the_same_seed_is_byte_identical(self):
        first = run_rollout({role: _greedy(role) for role in ROLES}, seed=42)
        second = run_rollout({role: _greedy(role) for role in ROLES}, seed=42)
        assert first.wire == second.wire
        assert first.final_hash == second.final_hash

    def test_replay_is_byte_identical_with_a_seeded_rng_tie_break(self):
        def once() -> Rollout:
            rng = Rng(9001)
            return run_rollout({role: _greedy(role, rng) for role in ROLES}, seed=42)

        first, second = once(), once()
        assert first.wire == second.wire
        assert first.final_hash == second.final_hash

    def test_every_emitted_envelope_is_accepted_by_the_validator(self):
        # run_rollout asserts acceptance per action; this pins that it
        # actually exercised the validator rather than emitting nothing.
        rollout = run_rollout({role: _greedy(role) for role in ROLES}, seed=42)
        assert rollout.envelopes
        assert {e.tool for e in rollout.envelopes} >= {"prepare", "serve", "coordinate"}

    def test_a_rollout_replays_action_for_action_into_a_fresh_sim(self):
        rollout = run_rollout({role: _greedy(role) for role in ROLES}, seed=42)
        # The envelopes alone, replayed tick by tick, must land on the same
        # state hash -- that is what a recorded trajectory is worth.
        replay = _sim()
        state = replay.reset(seed=42)
        pending = list(rollout.envelopes)
        while state.episode.phase == "active":
            while pending and pending[0].tick == state.episode.tick:
                result = replay.act(pending.pop(0))
                assert isinstance(result, RestaurantState)
                state = result
            state = replay.advance_time()
        assert not pending
        assert state_hash(state.model_dump(by_alias=True)) == rollout.final_hash

    def test_the_episode_runs_to_max_ticks(self):
        rollout = run_rollout({role: _greedy(role) for role in ROLES}, seed=42)
        assert rollout.final_state.episode.phase == "complete"
        assert (
            rollout.final_state.episode.tick == rollout.final_state.episode.max_ticks
        )


class TestIdleRollout:
    def test_idle_emits_nothing_and_the_episode_still_completes(self):
        rollout = run_rollout({role: _idle(role) for role in ROLES}, seed=42)
        assert rollout.envelopes == []
        assert rollout.final_state.episode.phase == "complete"
        assert (
            rollout.final_state.episode.tick == rollout.final_state.episode.max_ticks
        )

    def test_idle_serves_nothing(self):
        rollout = run_rollout({role: _idle(role) for role in ROLES}, seed=42)
        assert rollout.final_state.outcomes.completed == 0
        assert rollout.final_state.outcomes.cash == 0.0


class TestGreedyBeatsIdle:
    def test_greedy_completes_orders_idle_leaves_on_the_pass(self):
        greedy = run_rollout({role: _greedy(role) for role in ROLES}, seed=42)
        idle = run_rollout({role: _idle(role) for role in ROLES}, seed=42)
        assert greedy.final_state.outcomes.completed > idle.final_state.outcomes.completed
        assert greedy.final_state.outcomes.cash > idle.final_state.outcomes.cash
        assert (
            greedy.final_state.outcomes.satisfaction
            >= idle.final_state.outcomes.satisfaction
        )

    def test_greedy_serves_the_allergy_order_verified(self):
        greedy = run_rollout({role: _greedy(role) for role in ROLES}, seed=42)
        # order-2 carries the peanut allergy; serving it at all is only legal
        # verified, and it must be counted as a safe order.
        served = {
            o.id for o in greedy.final_state.orders if o.status == "served"
        }
        assert "order-2" in served
        assert greedy.final_state.outcomes.safe_orders >= 1


class TestProjectionDiscipline:
    def test_a_policy_rejects_an_observation_for_another_role(self):
        sim = _sim_at_tick_zero()
        chef_policy = _greedy("chef")
        try:
            chef_policy.decide(sim.observe("server"))
        except ValueError as exc:
            assert "server" in str(exc)
        else:  # pragma: no cover - the assertion above is the contract
            raise AssertionError("expected a role mismatch to raise")

    def test_greedy_never_prepares_an_order_it_already_started(self):
        sim = _sim_at_tick_zero()
        policy = _greedy("chef")
        first = policy.decide(sim.observe("chef"))
        assert first is not None and first.tool == "prepare"
        assert isinstance(sim.act(first), RestaurantState)
        state = sim.advance_time()
        second = policy.decide(sim.observe("chef"))
        assert second is not None
        assert second.arguments.get("orderId") != first.arguments.get("orderId")
        assert isinstance(sim.act(second), RestaurantState)
        assert state.episode.tick == 1

    def test_greedy_host_seats_a_waiting_party_at_the_smallest_fitting_table(self):
        # The MVP projection never populates the host queue, so the seat
        # branch is exercised against a hand-built observation -- still only
        # the observation, never simulator state.
        sim = _sim_at_tick_zero()
        observation = HostObservation(
            tick=0,
            load=0.0,
            queue=(QueuedParty(party_id="party-1", size=2, wait_minutes=3),),
            tables=(
                AvailableTable(id="table-3", seats=2, status="empty"),
                AvailableTable(id="table-9", seats=8, status="empty"),
            ),
            wait_estimate_minutes=0,
        )
        envelope = _greedy("host").decide(observation)
        assert envelope is not None
        assert envelope.tool == "seat"
        assert envelope.arguments == {"tableId": "table-3", "partySize": 2}
        assert isinstance(sim.act(envelope), RestaurantState)

    def test_greedy_host_stands_down_with_an_empty_queue(self):
        sim = _sim_at_tick_zero()
        assert _greedy("host").decide(sim.observe("host")) is None
