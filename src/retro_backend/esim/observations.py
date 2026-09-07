"""Role-projected views of :class:`RestaurantState` (#57).

Each role sees only what ``docs/restaurant-arena-tools.md`` grants it. The
four functions below are pure projections: no I/O, no RNG, no mutation.
Field shapes are pinned field-for-field against the four JSON examples in
that doc, verified in ``tests/test_esim_observations.py``.
"""

from collections.abc import Sequence
from typing import Literal

from retro_backend.esim.contracts import (
    AvailableTable,
    ChefObservation,
    DeliveredMessage,
    ExpediterObservation,
    GuestConstraint,
    HandoffOrder,
    HostObservation,
    Observation,
    ObservedEquipment,
    ObservedIncident,
    ObservedOrder,
    ObservedTable,
    ObservedWorker,
    OrderStatus,
    RestaurantState,
    Role,
    ServerObservation,
    Table,
)

# Wait time has no dedicated state field yet; it is derived from how long a
# table's order has sat un-served, in ticks, since the state contract does
# not carry per-order timestamps. One tick is treated as one minute, matching
# ``Clock.speed`` at its default of 1.
#
# Public because "is this order still live?" is the same question outside this
# module -- the manager projection (#79) asks it of a table's order, and a
# second copy of the status set there would be free to drift out of step with
# the four worker projections below.
ACTIVE_ORDER_STATUSES = frozenset({"queued", "seated", "preparing", "ready"})


def _worker_load(state: RestaurantState, role: Role) -> float:
    """The load of the (single, MVP) worker occupying ``role``."""
    for worker in state.workers:
        if worker.role == role:
            return worker.load
    raise ValueError(f"no worker occupies role {role!r}")


def table_wait_minutes(state: RestaurantState, table: Table) -> int:
    if table.order_id is None:
        return 0
    order = next((o for o in state.orders if o.id == table.order_id), None)
    if order is None or order.status not in ACTIVE_ORDER_STATUSES:
        return 0
    # Placeholder until per-order seating timestamps exist: every active
    # order has waited exactly one tick's worth per elapsed clock minute
    # since the episode began. MVP has one dinner-rush episode, so this is
    # simply the current clock minute -- still deterministic and stable for
    # hashing, and easy to replace once orders carry their own seated_at.
    return state.clock.minute


def observe_chef(
    state: RestaurantState, messages: Sequence[DeliveredMessage] = ()
) -> ChefObservation:
    orders = tuple(
        ObservedOrder(id=o.id, items=o.items, allergy=o.allergy, priority=o.priority)
        for o in state.orders
        if o.status in ACTIVE_ORDER_STATUSES
    )
    equipment = tuple(
        ObservedEquipment(id=e.id, status=e.status) for e in state.equipment
    )
    return ChefObservation(
        tick=state.episode.tick,
        load=_worker_load(state, "chef"),
        messages=tuple(messages),
        orders=orders,
        inventory=state.inventory,
        equipment=equipment,
    )


def observe_server(
    state: RestaurantState, messages: Sequence[DeliveredMessage] = ()
) -> ServerObservation:
    tables = tuple(
        ObservedTable(
            id=t.id,
            order_id=t.order_id,
            status=_order_status_for_table(state, t),
            wait_minutes=table_wait_minutes(state, t),
        )
        for t in state.tables
        if t.status == "occupied"
    )
    constraints = tuple(
        GuestConstraint(order_id=o.id, kind="allergy", value=o.allergy)
        for o in state.orders
        if o.allergy is not None
    )
    return ServerObservation(
        tick=state.episode.tick,
        load=_worker_load(state, "server"),
        messages=tuple(messages),
        tables=tables,
        guest_constraints=constraints,
    )


def _order_status_for_table(state: RestaurantState, table: Table) -> OrderStatus:
    if table.order_id is None:
        # A seated-but-unordered table has no order to project a status
        # from; "queued" is the closest available OrderStatus and matches
        # what the table will read once an order is placed.
        return "queued"
    order = next(o for o in state.orders if o.id == table.order_id)
    return order.status


def observe_host(
    state: RestaurantState, messages: Sequence[DeliveredMessage] = ()
) -> HostObservation:
    # The MVP has no separate waiting-party queue yet: every table not yet
    # seated is empty capacity, not a queued party. wait_estimate_minutes is
    # 0 with no queue and rises with occupied-table pressure otherwise.
    empty = [t for t in state.tables if t.status == "empty"]
    occupied = [t for t in state.tables if t.status == "occupied"]
    wait_estimate = 0 if empty or not occupied else 5 * len(occupied)
    return HostObservation(
        tick=state.episode.tick,
        load=_worker_load(state, "host"),
        messages=tuple(messages),
        queue=(),
        tables=tuple(
            _available_table(t) for t in state.tables if t.status == "empty"
        ),
        wait_estimate_minutes=wait_estimate,
    )


def _available_table(table: Table) -> AvailableTable:
    return AvailableTable(id=table.id, seats=table.seats, status=table.status)


_Handoff = Literal["kitchen", "pass", "floor"]
_HANDOFF_BY_ORDER_STATUS: dict[OrderStatus, _Handoff] = {
    "queued": "kitchen",
    "seated": "kitchen",
    "preparing": "kitchen",
    "ready": "pass",
    "served": "floor",
    "cancelled": "floor",
}


def observe_expediter(
    state: RestaurantState, messages: Sequence[DeliveredMessage] = ()
) -> ExpediterObservation:
    orders = tuple(
        HandoffOrder(
            id=o.id,
            status=o.status,
            priority=o.priority,
            handoff=_HANDOFF_BY_ORDER_STATUS[o.status],
        )
        for o in state.orders
        if o.status in ACTIVE_ORDER_STATUSES
    )
    workers = tuple(
        ObservedWorker(id=w.id, role=w.role, load=w.load) for w in state.workers
    )
    incidents = _observed_incidents(state)
    return ExpediterObservation(
        tick=state.episode.tick,
        load=_worker_load(state, "expediter"),
        messages=tuple(messages),
        orders=orders,
        workers=workers,
        incidents=incidents,
    )


def _observed_incidents(state: RestaurantState) -> tuple[ObservedIncident, ...]:
    incidents: list[ObservedIncident] = []
    if state.emergency is not None:
        incidents.append(
            ObservedIncident(kind=state.emergency.kind, active=state.emergency.active)
        )
    for equipment in state.equipment:
        if equipment.kind == "warming_shelf" and equipment.status != "working":
            incidents.append(
                ObservedIncident(kind="warming_shelf_overheat", active=True)
            )
    return tuple(incidents)


_OBSERVERS = {
    "chef": observe_chef,
    "server": observe_server,
    "host": observe_host,
    "expediter": observe_expediter,
}


def observe(
    state: RestaurantState,
    role: Role,
    messages: Sequence[DeliveredMessage] = (),
) -> Observation:
    """Project ``state`` into the observation ``role`` is allowed to see.

    ``messages`` is the role's inbox for this tick. It is passed in rather
    than read from ``state`` because ``restaurant-arena/v1`` state has nowhere
    to put it -- the simulator owns the inbox, the same way it owns prep
    timers. Passing it keeps this module a pure projection: same arguments,
    same observation, no mutation and no draining as a side effect of looking.
    """
    return _OBSERVERS[role](state, messages)
