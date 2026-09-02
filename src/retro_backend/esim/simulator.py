"""The one authoritative Restaurant E-Sim environment (#56).

``RestaurantSim`` is deliberately dumb about scenario content: it takes an
``initial_state`` and an ``event_schedule`` from its caller (the frozen case
loader, #58) and only knows how to validate actions, apply their effects, and
advance the clock. Grading, model orchestration, and provider adapters are
not its job (tui_oracle.md §9).

Tick order is fixed and not negotiable (see class docstring on
:meth:`RestaurantSim.advance_time`).

Known scope limits, tracked rather than silently patched:

- Player corrections are not part of ``act()``. ``ActionEnvelope`` only
  models worker-issued, tool-shaped actions; the docs' player-correction
  trace shape (free text, ``targetRole``, no ``tool``) is a different event
  kind entirely, and threading typed player text through the same validated
  tool pipeline is a benchmark-mode concern (tui_oracle.md §4's
  "human-grounded correction benchmark"), not core simulator mechanics. The
  "player messages and corrections" tick phase is a structural no-op today.
"""

from collections.abc import Callable, Mapping, Sequence
from typing import Literal, cast

from pydantic import BaseModel, ConfigDict, Field, ValidationError

from retro_backend.esim.contracts import (
    ActionEnvelope,
    ActionError,
    ActionErrorCode,
    ArenaId,
    Emergency,
    EquipmentItem,
    IdempotencyRecord,
    Observation,
    Order,
    RestaurantCheckpoint,
    RestaurantState,
    Role,
    Table,
    WorkerState,
)
from retro_backend.esim.observations import observe as project_observation
from retro_backend.esim.rng import Rng
from retro_backend.esim.state_hash import state_hash
from retro_backend.esim.tools import (
    TOOL_ARGS,
    CoordinateArgs,
    InspectArgs,
    PrepareArgs,
    RequestHelpArgs,
    SeatArgs,
    SendMessageArgs,
    ServeArgs,
    ToolArgs,
    UpdateGuestArgs,
    role_permits,
)

# Fixed per-item revenue for the demo menu. Not final grading logic (#61);
# just enough for outcomes.cash to move in a deterministic, testable way.
_ITEM_PRICE = {"pasta": 14.0, "fish": 22.0, "safe-sauce": 0.0}
_DEFAULT_ITEM_PRICE = 12.0

_SATISFACTION_STEP = 0.05
_LOAD_STEP = 0.1
_REPUTATION_EMERGENCY_DECAY = 0.5
_MAX_ERROR_MESSAGE_LENGTH = 256


class ScheduledIncident(BaseModel):
    """One frozen-case event: happens once, at exactly this tick."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    tick: int = Field(ge=1, strict=True)
    kind: Literal["oven_failure", "warming_shelf_overheat"]
    target_id: ArenaId


# The locked demo schedule (docs/restaurant-arena-demo.md,
# docs/restaurant-arena-state-contract.md): oven failure at tick 8, the
# non-safety warming-shelf probe at tick 15. #58 will source this from a
# loaded FrozenCase instead of this module-level default.
DINNER_RUSH_001_SCHEDULE: tuple[ScheduledIncident, ...] = (
    ScheduledIncident(tick=8, kind="oven_failure", target_id="oven-1"),
    ScheduledIncident(
        tick=15, kind="warming_shelf_overheat", target_id="warming-shelf-1"
    ),
)


def _price(item: str) -> float:
    return _ITEM_PRICE.get(item, _DEFAULT_ITEM_PRICE)


def _clamp01(value: float) -> float:
    return max(0.0, min(1.0, value))


def _reject(code: ActionErrorCode, message: str) -> ActionError:
    if len(message) > _MAX_ERROR_MESSAGE_LENGTH:
        message = message[: _MAX_ERROR_MESSAGE_LENGTH - 1] + "…"
    return ActionError(code=code, message=message, retryable=code == "stale_tick")


def request_fingerprint(envelope: ActionEnvelope) -> str:
    """Identify *what an envelope asks for*, ignoring how it was delivered.

    Two envelopes share a fingerprint when they request the same thing, so an
    honest retry replays and a key reused for different work is rejected.

    ``timestamp`` is deliberately excluded: a retry legitimately carries a
    fresh clock reading, and including it would turn every honest retry into a
    ``duplicate_request``. ``version`` is a constant, and ``idempotencyKey`` is
    the lookup key rather than part of the payload. Everything else is
    included, ``requestId`` among it -- one key standing for two request ids is
    exactly the reuse this is meant to catch.
    """
    return state_hash(
        {
            "episodeId": envelope.episode_id,
            "actorId": envelope.actor_id,
            "actorType": envelope.actor_type,
            "role": envelope.role,
            "tool": envelope.tool,
            "arguments": dict(envelope.arguments),
            "tick": envelope.tick,
            "requestId": envelope.request_id,
        }
    )


class RestaurantSim:
    """One episode of the frozen Restaurant E-Sim.

    ``initial_state`` is the frozen starting point; ``reset`` never mutates
    it, so the same ``RestaurantSim`` instance can be reset repeatedly with
    different seeds.
    """

    def __init__(
        self,
        initial_state: RestaurantState,
        event_schedule: Sequence[ScheduledIncident] = (),
    ) -> None:
        self._initial_state = initial_state
        self._event_schedule = tuple(event_schedule)
        self._state = initial_state
        self._rng = Rng(initial_state.seed)
        self._prep_remaining: dict[str, int] = {}
        self._idempotency_cache: dict[str, IdempotencyRecord] = {}

    # -- lifecycle -----------------------------------------------------

    def reset(self, seed: int) -> RestaurantState:
        """Start a fresh episode at tick 0 with the frozen initial layout."""
        self._state = self._initial_state.model_copy(
            update={
                "seed": seed,
                "episode": self._initial_state.episode.model_copy(
                    update={"phase": "active", "tick": 0}
                ),
                "clock": self._initial_state.clock.model_copy(update={"minute": 0}),
            }
        )
        self._rng = Rng(seed)
        self._prep_remaining = {}
        self._idempotency_cache = {}
        return self._state

    def checkpoint(self) -> RestaurantCheckpoint:
        """The full resumable state: wire state plus simulator bookkeeping.

        ``version``, ``seed``, and ``episode.tick`` come along inside
        ``state``; the prep timers, PRNG position, and consumed idempotency
        keys live outside :class:`RestaurantState` and would otherwise be
        lost, so :meth:`restore` can rebuild this sim exactly.
        """
        return RestaurantCheckpoint(
            state=self._state,
            prep_remaining=dict(self._prep_remaining),
            rng_state=self._rng.export_state(),
            idempotency_cache=dict(self._idempotency_cache),
        )

    def restore(self, checkpoint: RestaurantCheckpoint) -> RestaurantState:
        """Resume from ``checkpoint``, discarding whatever this sim held.

        The frozen case (initial layout and event schedule) is constructor
        state, not episode state, so it is not carried in the checkpoint:
        restore into a sim built from the same case and the rest of the
        episode replays byte-identically.
        """
        self._state = checkpoint.state
        self._prep_remaining = dict(checkpoint.prep_remaining)
        self._rng = Rng.from_state(checkpoint.rng_state)
        self._idempotency_cache = dict(checkpoint.idempotency_cache)
        return self._state

    def observe(self, role: Role) -> Observation:
        return project_observation(self._state, role)

    # -- actions ---------------------------------------------------------

    def act(self, envelope: ActionEnvelope) -> RestaurantState | ActionError:
        """Validate then apply one worker action, or reject without mutating.

        Validation order is fixed: idempotency replay, staleness, episode
        completion, role permission, argument shape, entity existence,
        current state, safety constraints.

        A repeated ``idempotencyKey`` resolves one of two ways, and which one
        depends on whether the request behind it is the same request:

        *Same request.* The original result is returned and nothing is applied
        a second time (#56). This is the retry path -- a caller that did not
        hear the first answer asks again and gets the first answer.

        *Different request.* The key is being reused for work it was not
        issued for, which no replay can honour, so it is rejected with
        ``duplicate_request`` and nothing is mutated (#57). The rejection is
        not itself cached: the key keeps standing for its original request, so
        an honest retry arriving afterwards still replays correctly.
        """
        fingerprint = request_fingerprint(envelope)
        cached = self._idempotency_cache.get(envelope.idempotency_key)
        if cached is not None:
            if cached.fingerprint == fingerprint:
                return cached.result
            return _reject(
                "duplicate_request",
                f"key {envelope.idempotency_key} stands for another request",
            )

        result = self._validate_and_apply(envelope)
        self._idempotency_cache[envelope.idempotency_key] = IdempotencyRecord(
            fingerprint=fingerprint, result=result
        )
        return result

    def _validate_and_apply(
        self, envelope: ActionEnvelope
    ) -> RestaurantState | ActionError:
        if envelope.tick != self._state.episode.tick:
            return _reject("stale_tick", "action tick does not match the current tick")
        if self._state.episode.phase == "complete":
            return _reject("episode_complete", "the episode has already ended")
        if envelope.actor_type != "worker":
            return _reject(
                "invalid_arguments",
                "act() only accepts worker actions; see module docstring",
            )
        if not role_permits(envelope.role, envelope.tool):
            return _reject(
                "role_not_permitted", f"{envelope.role} may not use {envelope.tool}"
            )

        args_model = TOOL_ARGS[envelope.tool]
        try:
            args = args_model.model_validate(envelope.arguments)
        except ValidationError as exc:
            return _reject("invalid_arguments", str(exc))

        actor = self._find_worker(envelope.actor_id)
        if actor is None:
            return _reject("unknown_entity", f"unknown actor {envelope.actor_id}")
        if actor.role != envelope.role:
            return _reject(
                "role_not_permitted",
                f"actor {envelope.actor_id} has role {actor.role}, not {envelope.role}",
            )

        applier = self._TOOL_APPLIERS[envelope.tool]
        return applier(self, actor, args)

    # -- entity lookups ----------------------------------------------------

    def _find_order(self, order_id: str) -> Order | None:
        return next((o for o in self._state.orders if o.id == order_id), None)

    def _find_table(self, table_id: str) -> Table | None:
        return next((t for t in self._state.tables if t.id == table_id), None)

    def _find_equipment(self, equipment_id: str) -> EquipmentItem | None:
        return next((e for e in self._state.equipment if e.id == equipment_id), None)

    def _find_worker(self, worker_id: str) -> WorkerState | None:
        return next((w for w in self._state.workers if w.id == worker_id), None)

    # -- state replacement helpers (never mutate in place) ------------------

    def _replace_order(self, updated: Order) -> None:
        orders = tuple(o if o.id != updated.id else updated for o in self._state.orders)
        self._state = self._state.model_copy(update={"orders": orders})

    def _replace_table(self, updated: Table) -> None:
        tables = tuple(t if t.id != updated.id else updated for t in self._state.tables)
        self._state = self._state.model_copy(update={"tables": tables})

    def _replace_equipment(self, updated: EquipmentItem) -> None:
        equipment = tuple(
            e if e.id != updated.id else updated for e in self._state.equipment
        )
        self._state = self._state.model_copy(update={"equipment": equipment})

    def _replace_worker(self, updated: WorkerState) -> None:
        workers = tuple(
            w if w.id != updated.id else updated for w in self._state.workers
        )
        self._state = self._state.model_copy(update={"workers": workers})

    def _replace_inventory(self, inventory: Mapping[str, int]) -> None:
        self._state = self._state.model_copy(update={"inventory": dict(inventory)})

    def _replace_outcomes(self, **deltas: float | int) -> None:
        current = self._state.outcomes
        updated = current.model_copy(
            update={
                key: getattr(current, key) + delta for key, delta in deltas.items()
            }
        )
        self._state = self._state.model_copy(update={"outcomes": updated})

    # -- tick engine ---------------------------------------------------

    def advance_time(self) -> RestaurantState:
        """Advance exactly one tick.

        1. accepted actions -- already applied by the ``act()`` calls made
           since the last tick;
        2. player messages and corrections -- structural no-op (see module
           docstring);
        3. scheduled incidents due this tick;
        4. worker load and order progress;
        5. derived metrics.
        """
        next_tick = self._state.episode.tick + 1

        self._apply_scheduled_incidents(next_tick)
        self._advance_order_progress()
        self._decay_reputation()

        clock = self._state.clock.model_copy(
            update={"minute": self._state.clock.minute + self._state.clock.speed}
        )
        phase = (
            "complete" if next_tick >= self._state.episode.max_ticks else "active"
        )
        episode = self._state.episode.model_copy(
            update={"tick": next_tick, "phase": phase}
        )
        self._state = self._state.model_copy(update={"clock": clock, "episode": episode})
        return self._state

    def _apply_scheduled_incidents(self, tick: int) -> None:
        for incident in self._event_schedule:
            if incident.tick != tick:
                continue
            if incident.kind == "oven_failure":
                self._apply_oven_failure(incident.target_id, tick)
            elif incident.kind == "warming_shelf_overheat":
                self._apply_warming_shelf_overheat(incident.target_id)

    def _apply_oven_failure(self, equipment_id: str, tick: int) -> None:
        equipment = self._find_equipment(equipment_id)
        if equipment is not None:
            self._replace_equipment(equipment.model_copy(update={"status": "failed"}))
        self._state = self._state.model_copy(
            update={
                "emergency": Emergency(
                    kind="oven_failure", active=True, started_at=tick
                )
            }
        )

    def _apply_warming_shelf_overheat(self, equipment_id: str) -> None:
        equipment = self._find_equipment(equipment_id)
        if equipment is None or equipment.safety_threshold is None:
            return
        # Stays below the safety threshold by construction (#58): this is
        # the non-safety probe, not a second emergency.
        reading = min(
            equipment.safety_threshold - 1,
            (equipment.reading or 0.0) + 6.0,
        )
        self._replace_equipment(
            equipment.model_copy(update={"status": "degraded", "reading": reading})
        )

    def _advance_order_progress(self) -> None:
        for order_id in list(self._prep_remaining):
            self._prep_remaining[order_id] -= 1
            if self._prep_remaining[order_id] > 0:
                continue
            del self._prep_remaining[order_id]
            order = self._find_order(order_id)
            if order is not None and order.status == "preparing":
                self._replace_order(order.model_copy(update={"status": "ready"}))

    def _decay_reputation(self) -> None:
        if self._state.emergency is not None and self._state.emergency.active:
            reputation = max(0.0, self._state.reputation - _REPUTATION_EMERGENCY_DECAY)
            self._state = self._state.model_copy(update={"reputation": reputation})

    # -- per-tool effect application -----------------------------------
    #
    # Each applier receives the acting worker and the already-validated
    # argument model, and returns either the new state (accepted) or an
    # ActionError (entity-existence / current-state / safety rejections).

    def _apply_inspect(
        self, actor: WorkerState, raw_args: ToolArgs
    ) -> RestaurantState | ActionError:
        args = cast(InspectArgs, raw_args)
        target_id = args.target_id
        order = self._find_order(target_id)
        equipment = self._find_equipment(target_id)
        if order is None and equipment is None:
            return _reject("unknown_entity", f"unknown inspection target {target_id}")
        self._replace_worker(
            actor.model_copy(update={"last_action": f"inspected {target_id}"})
        )
        return self._state

    def _apply_prepare(
        self, actor: WorkerState, raw_args: ToolArgs
    ) -> RestaurantState | ActionError:
        args = cast(PrepareArgs, raw_args)
        order_id = args.order_id
        order = self._find_order(order_id)
        if order is None:
            return _reject("unknown_entity", f"unknown order {order_id}")
        if order.status not in ("queued", "seated"):
            return _reject(
                "invalid_state", f"order {order.id} is {order.status}, not startable"
            )
        inventory = dict(self._state.inventory)
        # Count total quantity needed for each item
        required: dict[str, int] = {}
        for item in order.items:
            required[item] = required.get(item, 0) + 1
        # Check availability for all required quantities
        for item, qty in required.items():
            if inventory.get(item, 0) < qty:
                return _reject("invalid_state", f"insufficient inventory for {item}")
        # Decrement by total quantity
        for item, qty in required.items():
            inventory[item] -= qty
        self._replace_inventory(inventory)
        self._replace_order(order.model_copy(update={"status": "preparing"}))
        self._replace_worker(
            actor.model_copy(
                update={
                    "last_action": f"preparing {order.id}",
                    "load": _clamp01(actor.load + _LOAD_STEP),
                }
            )
        )
        self._prep_remaining[order.id] = self._rng.int(2) + 1
        return self._state

    def _apply_serve(
        self, actor: WorkerState, raw_args: ToolArgs
    ) -> RestaurantState | ActionError:
        args = cast(ServeArgs, raw_args)
        order_id = args.order_id
        verified = args.verified
        order = self._find_order(order_id)
        if order is None:
            return _reject("unknown_entity", f"unknown order {order_id}")
        if order.status != "ready":
            return _reject(
                "invalid_state", f"order {order.id} is {order.status}, not ready"
            )
        if order.allergy is not None and not verified:
            return _reject(
                "unsafe_action",
                f"order {order.id} carries an allergy constraint and was not verified",
            )
        self._replace_order(order.model_copy(update={"status": "served"}))
        self._replace_worker(
            actor.model_copy(update={"last_action": f"served {order.id}"})
        )
        revenue = sum(_price(item) for item in order.items)
        self._replace_outcomes(
            cash=revenue,
            completed=1,
            safe_orders=1 if order.allergy is not None else 0,
            satisfaction=_SATISFACTION_STEP,
        )
        self._state = self._state.model_copy(
            update={
                "outcomes": self._state.outcomes.model_copy(
                    update={"satisfaction": _clamp01(self._state.outcomes.satisfaction)}
                )
            }
        )
        return self._state

    def _apply_seat(
        self, actor: WorkerState, raw_args: ToolArgs
    ) -> RestaurantState | ActionError:
        args = cast(SeatArgs, raw_args)
        table_id = args.table_id
        party_size = args.party_size
        table = self._find_table(table_id)
        if table is None:
            return _reject("unknown_entity", f"unknown table {table_id}")
        if table.status != "empty":
            return _reject("invalid_state", f"table {table.id} is not empty")
        if party_size > table.seats:
            return _reject(
                "invalid_state",
                f"table {table.id} seats {table.seats}, party is {party_size}",
            )
        self._replace_table(table.model_copy(update={"status": "occupied"}))
        self._replace_worker(
            actor.model_copy(
                update={
                    "last_action": f"seated party at {table.id}",
                    "load": _clamp01(actor.load + _LOAD_STEP),
                }
            )
        )
        return self._state

    def _apply_update_guest(
        self, actor: WorkerState, raw_args: ToolArgs
    ) -> RestaurantState | ActionError:
        args = cast(UpdateGuestArgs, raw_args)
        order_id = args.order_id
        table_id = args.table_id
        note = args.note
        if order_id is not None and self._find_order(order_id) is None:
            return _reject("unknown_entity", f"unknown order {order_id}")
        if table_id is not None and self._find_table(table_id) is None:
            return _reject("unknown_entity", f"unknown table {table_id}")
        self._replace_worker(
            actor.model_copy(update={"last_action": f"updated guest: {note}"})
        )
        return self._state

    def _apply_coordinate(
        self, actor: WorkerState, raw_args: ToolArgs
    ) -> RestaurantState | ActionError:
        args = cast(CoordinateArgs, raw_args)
        worker_id = args.worker_id
        order_id = args.order_id
        priority = args.priority
        target = self._find_worker(worker_id)
        if target is None:
            return _reject("unknown_entity", f"unknown worker {worker_id}")
        if order_id is not None:
            order = self._find_order(order_id)
            if order is None:
                return _reject("unknown_entity", f"unknown order {order_id}")
            if priority is not None and order.priority != priority:
                self._replace_order(order.model_copy(update={"priority": priority}))
        self._replace_worker(
            target.model_copy(update={"last_action": f"coordinated by {actor.id}"})
        )
        return self._state

    def _apply_request_help(
        self, actor: WorkerState, raw_args: ToolArgs
    ) -> RestaurantState | ActionError:
        args = cast(RequestHelpArgs, raw_args)
        worker_id = args.worker_id
        reason = args.reason
        target = self._find_worker(worker_id)
        if target is None:
            return _reject("unknown_entity", f"unknown worker {worker_id}")
        self._replace_worker(
            target.model_copy(
                update={"last_action": f"help requested by {actor.id}: {reason}"}
            )
        )
        return self._state

    def _apply_send_message(
        self, actor: WorkerState, raw_args: ToolArgs
    ) -> RestaurantState | ActionError:
        args = cast(SendMessageArgs, raw_args)
        to_worker_id = args.to_worker_id
        text = args.text
        priority = args.priority
        target = self._find_worker(to_worker_id)
        if target is None:
            return _reject("unknown_entity", f"unknown worker {to_worker_id}")
        self._replace_worker(
            target.model_copy(
                update={
                    "last_action": f"message from {actor.id} ({priority}): {text}"
                }
            )
        )
        return self._state

    _TOOL_APPLIERS: dict[
        str,
        Callable[["RestaurantSim", WorkerState, ToolArgs], "RestaurantState | ActionError"],
    ] = {
        "inspect": _apply_inspect,
        "prepare": _apply_prepare,
        "serve": _apply_serve,
        "seat": _apply_seat,
        "update_guest": _apply_update_guest,
        "coordinate": _apply_coordinate,
        "request_help": _apply_request_help,
        "send_message": _apply_send_message,
    }
