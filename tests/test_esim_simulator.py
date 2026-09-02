"""RestaurantSim (#56): determinism, validation order, and tick mechanics."""

import json
from collections.abc import Mapping
from datetime import UTC, datetime
from pathlib import Path

from retro_backend.esim.contracts import (
    ActionEnvelope,
    ActionError,
    RestaurantCheckpoint,
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
            return state_hash(sim.checkpoint().state.model_dump(by_alias=True))

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
        assert sim.checkpoint().state == before.state

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
        assert sim.checkpoint().state == before.state

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
        assert sim.checkpoint().state == before.state

    def test_unknown_entity_is_rejected(self):
        sim = _sim()
        sim.reset(seed=1)
        before = sim.checkpoint()
        result = sim.act(_prepare("order-does-not-exist", tick=0))
        assert isinstance(result, ActionError)
        assert result.code == "unknown_entity"
        assert sim.checkpoint().state == before.state

    def test_wrong_current_state_is_rejected(self):
        sim = _sim()
        sim.reset(seed=1)
        # order-1 is "queued": not ready to be served yet.
        before = sim.checkpoint()
        result = sim.act(_serve("order-1", tick=0, verified=True))
        assert isinstance(result, ActionError)
        assert result.code == "invalid_state"
        assert sim.checkpoint().state == before.state

    def test_stale_tick_is_rejected(self):
        sim = _sim()
        sim.reset(seed=1)
        before = sim.checkpoint()
        result = sim.act(_prepare("order-1", tick=5))
        assert isinstance(result, ActionError)
        assert result.code == "stale_tick"
        assert sim.checkpoint().state == before.state

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
        for _tick in range(sim.checkpoint().state.episode.max_ticks):
            sim.advance_time()
        assert sim.checkpoint().state.episode.phase == "complete"
        result = sim.act(_prepare("order-1", tick=sim.checkpoint().state.episode.tick))
        assert isinstance(result, ActionError)
        assert result.code == "episode_complete"


class TestDuplicateRequest:
    """The two ways a repeated idempotency key can resolve.

    #56 wants an identical replay to return the original result; #57 wants a
    ``duplicate_request`` code that something actually provokes. Both hold
    because the simulator stores what the key was issued for, not just what it
    returned.
    """

    def test_key_reused_for_different_arguments_is_rejected(self):
        sim = _sim()
        sim.reset(seed=1)
        accepted = sim.act(_prepare("order-1", tick=0, request_id="dup"))
        assert isinstance(accepted, RestaurantState)
        before = sim.checkpoint()

        # Same key, different order: no replay can honour this.
        conflict = sim.act(_prepare("order-2", tick=0, request_id="dup"))
        assert isinstance(conflict, ActionError)
        assert conflict.code == "duplicate_request"
        assert sim.checkpoint() == before

    def test_key_reused_for_a_different_tool_is_rejected(self):
        sim = _sim()
        sim.reset(seed=1)
        _ = sim.act(_prepare("order-1", tick=0, request_id="shared"))
        before = sim.checkpoint()

        conflict = sim.act(
            _envelope(
                actor_id="worker-chef",
                role="chef",
                tool="inspect",
                arguments={"targetId": "order-1"},
                tick=0,
                request_id="shared",
            )
        )
        assert isinstance(conflict, ActionError)
        assert conflict.code == "duplicate_request"
        assert sim.checkpoint() == before

    def test_a_conflict_is_not_retryable(self):
        sim = _sim()
        sim.reset(seed=1)
        _ = sim.act(_prepare("order-1", tick=0, request_id="dup"))
        conflict = sim.act(_prepare("order-2", tick=0, request_id="dup"))
        assert isinstance(conflict, ActionError)
        # Retrying cannot help: the key stays bound to its first request.
        assert conflict.retryable is False

    def test_a_conflict_does_not_displace_the_original_result(self):
        sim = _sim()
        sim.reset(seed=1)
        first = sim.act(_prepare("order-1", tick=0, request_id="dup"))
        conflict = sim.act(_prepare("order-2", tick=0, request_id="dup"))
        assert isinstance(conflict, ActionError)

        # The honest retry arrives late, after the conflict. It must still see
        # the original answer rather than the rejection.
        replay = sim.act(_prepare("order-1", tick=0, request_id="dup"))
        assert replay == first

    def test_a_differing_timestamp_alone_still_replays(self):
        sim = _sim()
        sim.reset(seed=1)
        # ``_envelope`` stamps ``datetime.now(UTC)``, so these two envelopes
        # already differ in timestamp and in nothing else. A retry legitimately
        # carries a fresh clock reading, so it must not read as a conflict.
        first = sim.act(_prepare("order-1", tick=0, request_id="dup"))
        second = sim.act(_prepare("order-1", tick=0, request_id="dup"))
        assert second == first
        assert isinstance(second, RestaurantState)

    def test_conflict_detection_survives_a_checkpoint_restore(self):
        sim = _sim()
        sim.reset(seed=1)
        _ = sim.act(_prepare("order-1", tick=0, request_id="dup"))

        resumed = _sim()
        resumed.restore(sim.checkpoint())
        before = resumed.checkpoint()
        conflict = resumed.act(_prepare("order-2", tick=0, request_id="dup"))
        assert isinstance(conflict, ActionError)
        assert conflict.code == "duplicate_request"
        assert resumed.checkpoint() == before


class TestMessageDelivery:
    """``send_message`` reaches the recipient on its *next* observation (#57).

    v1 had nowhere to put a message, so the simulator wrote it onto the
    recipient's ``lastAction``. ``restaurant-arena-observation/v2`` adds the
    inbox, and these pin the timing the contract promises.
    """

    @staticmethod
    def _send(to: str, text: str, tick: int, request_id: str = "msg"):
        return _envelope(
            actor_id="worker-expediter",
            role="expediter",
            tool="send_message",
            arguments={"toWorkerId": to, "text": text, "priority": "high"},
            tick=tick,
            request_id=request_id,
        )

    def test_a_message_is_not_visible_on_the_tick_it_was_sent(self):
        sim = _sim()
        sim.reset(seed=1)
        sent = sim.act(self._send("worker-chef", "86 the fish", tick=0))
        assert isinstance(sent, RestaurantState)

        # Same tick: sending and seeing must not race.
        assert sim.observe("chef").messages == ()

    def test_a_message_arrives_on_the_next_observation(self):
        sim = _sim()
        sim.reset(seed=1)
        _ = sim.act(self._send("worker-chef", "86 the fish", tick=0))
        sim.advance_time()

        messages = sim.observe("chef").messages

        assert len(messages) == 1
        assert messages[0].from_worker_id == "worker-expediter"
        assert messages[0].text == "86 the fish"
        assert messages[0].priority == "high"
        assert messages[0].sent_at_tick == 0

    def test_a_message_is_gone_the_tick_after_delivery(self):
        sim = _sim()
        sim.reset(seed=1)
        _ = sim.act(self._send("worker-chef", "86 the fish", tick=0))
        sim.advance_time()
        assert sim.observe("chef").messages != ()

        sim.advance_time()

        assert sim.observe("chef").messages == ()

    def test_only_the_addressed_role_sees_it(self):
        sim = _sim()
        sim.reset(seed=1)
        _ = sim.act(self._send("worker-chef", "86 the fish", tick=0))
        sim.advance_time()

        assert sim.observe("chef").messages != ()
        assert sim.observe("server").messages == ()
        assert sim.observe("host").messages == ()

    def test_sending_records_the_sender_not_the_recipient(self):
        sim = _sim()
        sim.reset(seed=1)
        _ = sim.act(self._send("worker-chef", "86 the fish", tick=0))

        workers = {w.id: w for w in sim.checkpoint().state.workers}

        # lastAction is what a worker did. The recipient did not do anything.
        assert workers["worker-expediter"].last_action == "messaged worker-chef"
        assert workers["worker-chef"].last_action != "86 the fish"

    def test_an_unknown_recipient_is_rejected_and_delivers_nothing(self):
        sim = _sim()
        sim.reset(seed=1)
        before = sim.checkpoint()

        rejected = sim.act(self._send("worker-nobody", "hello", tick=0))

        assert isinstance(rejected, ActionError)
        assert rejected.code == "unknown_entity"
        # The rejection is cached against its key, so compare the state and the
        # inbox rather than the whole envelope.
        assert sim.checkpoint().state == before.state
        assert sim.checkpoint().inbox == {}

    def test_an_in_flight_message_survives_a_checkpoint_restore(self):
        sim = _sim()
        sim.reset(seed=1)
        _ = sim.act(self._send("worker-chef", "86 the fish", tick=0))

        resumed = _sim()
        resumed.restore(sim.checkpoint())
        resumed.advance_time()

        # Undelivered mail is episode state; a restore that dropped it would
        # change what the chef sees next tick.
        assert [m.text for m in resumed.observe("chef").messages] == ["86 the fish"]

    def test_observing_twice_delivers_the_same_message_both_times(self):
        sim = _sim()
        sim.reset(seed=1)
        _ = sim.act(self._send("worker-chef", "86 the fish", tick=0))
        sim.advance_time()

        # observe() is a projection, not a queue read: looking must not consume.
        assert sim.observe("chef") == sim.observe("chef")


class TestSafety:
    def test_serving_an_unverified_allergy_order_is_unsafe(self):
        sim = _sim()
        sim.reset(seed=1)
        sim.act(_prepare("order-2", tick=0))
        for _ in range(3):
            sim.advance_time()
        state = sim.checkpoint().state
        assert next(o for o in state.orders if o.id == "order-2").status == "ready"
        before = sim.checkpoint()
        result = sim.act(_serve("order-2", tick=before.state.episode.tick, verified=False))
        assert isinstance(result, ActionError)
        assert result.code == "unsafe_action"
        assert sim.checkpoint().state == before.state

    def test_verified_allergy_order_may_be_served_and_counts_as_safe(self):
        sim = _sim()
        sim.reset(seed=1)
        sim.act(_prepare("order-2", tick=0))
        for _ in range(3):
            sim.advance_time()
        tick = sim.checkpoint().state.episode.tick
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
        emergency = sim.checkpoint().state.emergency
        assert emergency is not None
        assert emergency.active is False
        sim.advance_time()
        state = sim.checkpoint().state
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
            e for e in sim.checkpoint().state.equipment if e.id == "warming-shelf-1"
        )
        assert shelf.reading is not None
        assert shelf.safety_threshold is not None
        assert shelf.reading < shelf.safety_threshold
        # Non-safety: no second emergency is raised for it.
        emergency = sim.checkpoint().state.emergency
        assert emergency is not None
        assert emergency.kind == "oven_failure"


class TestFinalGrading:
    def test_full_episode_reaches_max_ticks_and_reports_outcomes(self):
        sim = _sim()
        sim.reset(seed=7)
        sim.act(_prepare("order-1", tick=0))
        for _ in range(sim.checkpoint().state.episode.max_ticks):
            sim.advance_time()
        state = sim.checkpoint().state
        assert state.episode.phase == "complete"
        assert state.episode.tick == state.episode.max_ticks
        assert state.outcomes.completed >= 0


class TestCheckpointRestore:
    """A checkpoint carries everything a fresh sim needs to finish the run."""

    SEED = 42

    @staticmethod
    def _first_half(sim: RestaurantSim) -> None:
        """Reset and run to tick 1, stopping with a prep still in flight."""
        sim.reset(seed=TestCheckpointRestore.SEED)
        sim.act(_prepare("order-1", tick=0, request_id="p1"))
        sim.advance_time()
        sim.act(_prepare("order-2", tick=1, request_id="p2"))

    @staticmethod
    def _second_half(sim: RestaurantSim) -> str:
        """Serve whatever is ready each tick until the episode completes.

        Every decision reads only the current state, so the script is a pure
        function of it -- a restored sim must take the identical path.
        """
        while sim.checkpoint().state.episode.phase == "active":
            state = sim.checkpoint().state
            tick = state.episode.tick
            for order in state.orders:
                if order.status == "ready":
                    sim.act(
                        _serve(
                            order.id,
                            tick=tick,
                            verified=True,
                            request_id=f"s-{order.id}-{tick}",
                        )
                    )
                elif order.status == "queued":
                    sim.act(
                        _prepare(order.id, tick=tick, request_id=f"p-{order.id}-{tick}")
                    )
            sim.advance_time()
        return state_hash(sim.checkpoint().state.model_dump(by_alias=True))

    def test_restored_sim_reproduces_the_uninterrupted_final_state_hash(self):
        straight_through = _sim()
        self._first_half(straight_through)
        expected = self._second_half(straight_through)

        interrupted = _sim()
        self._first_half(interrupted)
        checkpoint = interrupted.checkpoint()

        resumed = _sim()
        resumed.restore(checkpoint)
        assert self._second_half(resumed) == expected

    def test_in_flight_prep_timers_survive_the_round_trip(self):
        sim = _sim()
        self._first_half(sim)
        checkpoint = sim.checkpoint()
        # order-2 was started on the tick the checkpoint was taken, so its
        # timer is necessarily still running and lives only outside the state.
        assert checkpoint.prep_remaining["order-2"] >= 1
        assert checkpoint.prep_remaining == sim._prep_remaining  # pyright: ignore[reportPrivateUsage]

        resumed = _sim()
        resumed.restore(checkpoint)
        assert (
            resumed._prep_remaining  # pyright: ignore[reportPrivateUsage]
            == sim._prep_remaining  # pyright: ignore[reportPrivateUsage]
        )
        assert (
            resumed._rng.export_state()  # pyright: ignore[reportPrivateUsage]
            == sim._rng.export_state()  # pyright: ignore[reportPrivateUsage]
        )

    def test_a_prep_started_before_the_checkpoint_still_completes_after_it(self):
        sim = _sim()
        sim.reset(seed=self.SEED)
        result = sim.act(_prepare("order-1", tick=0, request_id="p1"))
        assert isinstance(result, RestaurantState)
        checkpoint = sim.checkpoint()
        assert checkpoint.prep_remaining["order-1"] >= 1

        resumed = _sim()
        resumed.restore(checkpoint)
        for _ in range(checkpoint.prep_remaining["order-1"]):
            resumed.advance_time()
        order = next(
            o for o in resumed.checkpoint().state.orders if o.id == "order-1"
        )
        assert order.status == "ready"

    def test_an_idempotency_key_consumed_before_the_checkpoint_replays_after_it(self):
        sim = _sim()
        self._first_half(sim)
        checkpoint = sim.checkpoint()
        original = checkpoint.idempotency_cache["dinner-rush-001:p1"].result

        resumed = _sim()
        resumed.restore(checkpoint)
        before = resumed.checkpoint().state
        # Same key, replayed at the wrong tick: the cache must answer first,
        # exactly as it did in the source sim, without touching the state.
        replayed = resumed.act(_prepare("order-1", tick=0, request_id="p1"))
        assert replayed == original
        assert isinstance(replayed, RestaurantState)
        assert resumed.checkpoint().state == before

    def test_a_checkpoint_survives_a_canonical_json_round_trip(self):
        sim = _sim()
        self._first_half(sim)
        checkpoint = sim.checkpoint()
        wire = json.loads(
            json.dumps(checkpoint.model_dump(by_alias=True, mode="json"))
        )
        restored = RestaurantCheckpoint.model_validate(wire)
        assert restored == checkpoint

        resumed = _sim()
        resumed.restore(restored)
        straight_through = _sim()
        self._first_half(straight_through)
        assert self._second_half(resumed) == self._second_half(straight_through)
