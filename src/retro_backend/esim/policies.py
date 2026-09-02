"""Deterministic reference policies for the frozen Restaurant E-Sim (#56).

Two policies live here: :class:`IdlePolicy`, which never acts, and
:class:`GreedyPolicy`, which takes the single most valuable action its role's
observation actually justifies. They exist so grader tests and determinism
sanity checks have a fixed, reproducible driver for ``RestaurantSim`` --
"replay this rollout twice and get the same bytes" needs *something* to issue
the actions, and a model is not reproducible enough to be that something.

**Infrastructure only.** These are never a stand-in for a model on the normal
path (tui-gamepigeon#221: "No normal-path scripted worker behavior;
deterministic fallback is infrastructure-only"). Nothing under ``src/`` may
import this module: not the simulator, not a route, not a worker, not a
provider adapter. Test code is the only permitted importer.

Two properties are load-bearing:

*Projection respect.* A policy is handed the :data:`Observation` its role is
allowed to see and nothing else. It never touches ``RestaurantSim`` internals
and never reads a :class:`RestaurantState`. Whatever the projection hides, the
policy has to cope without -- which is exactly what makes these useful as a
check that the observation carries enough to act on.

*Determinism.* Every choice is either a total order over stable fields
(priority rank, then id) or a draw from a caller-supplied seeded
:class:`~retro_backend.esim.rng.Rng`. No ``random``, no set or dict iteration
order, no ``id()``, no wall clock -- the envelope timestamp is derived from
the tick, so two rollouts from the same seed emit byte-identical envelopes.

One projection gap is worth naming: ``ChefObservation`` carries no order
status, so the chef cannot tell a ``queued`` order from one it already put in
the oven. :class:`GreedyPolicy` therefore remembers the orders it has asked to
prepare. That memory is the policy's own record of what it emitted, not a
peek at simulator state, and it is cleared by :meth:`GreedyPolicy.reset`.
"""

from collections.abc import Callable, Mapping, Sequence
from datetime import UTC, datetime, timedelta
from typing import Protocol, TypeVar, override

from retro_backend.esim.contracts import (
    ACTION_VERSION,
    ActionEnvelope,
    ChefObservation,
    ExpediterObservation,
    HostObservation,
    Observation,
    Priority,
    Role,
    ServerObservation,
    Tool,
)
from retro_backend.esim.rng import Rng

__all__ = [
    "POLICY_EPOCH",
    "GreedyPolicy",
    "IdlePolicy",
    "Policy",
    "idempotency_key_for",
    "request_id_for",
]

# Envelopes carry an AwareDatetime, and ``datetime.now()`` would make two
# replays of the same rollout differ byte-for-byte. The timestamp is derived
# from the tick instead, one minute per tick, matching Clock.speed's default.
POLICY_EPOCH = datetime(2026, 1, 1, tzinfo=UTC)

_PRIORITY_RANK: Mapping[Priority, int] = {"high": 0, "normal": 1}

# The one OrderStatus a server may act on: serve() rejects anything else.
_READY = "ready"

_T = TypeVar("_T")


def request_id_for(policy_name: str, role: Role, tick: int, tool: Tool) -> str:
    """The stable request id a policy uses for one (role, tick) decision.

    A policy emits at most one action per role per tick, so ``role`` plus
    ``tick`` is already unique within an episode; ``policy_name`` and ``tool``
    are carried for readability in traces. The result matches
    ``ARENA_ID_PATTERN`` -- no colon, so it composes into an idempotency key.
    """
    return f"{policy_name}-{role}-t{tick:04d}-{tool}"


def idempotency_key_for(episode_id: str, request_id: str) -> str:
    """``"<episodeId>:<requestId>"``, the spelling ``contracts`` documents."""
    return f"{episode_id}:{request_id}"


class Policy(Protocol):
    """What a rollout driver needs from a policy.

    ``decide`` receives only the observation projected for :attr:`role`. The
    current tick is ``observation.tick``; a policy has no other clock.
    """

    name: str
    role: Role
    actor_id: str

    def decide(self, observation: Observation) -> ActionEnvelope | None:
        """Return the action for this tick, or ``None`` to stand down."""
        ...

    def reset(self) -> None:
        """Drop any per-episode memory so the policy can be replayed."""
        ...


class _PolicyBase:
    """Envelope construction shared by the reference policies."""

    name: str = "policy"

    def __init__(
        self,
        *,
        episode_id: str,
        actor_id: str,
        role: Role,
        epoch: datetime = POLICY_EPOCH,
    ) -> None:
        self.episode_id: str = episode_id
        self.actor_id: str = actor_id
        self.role: Role = role
        self.epoch: datetime = epoch

    def reset(self) -> None:
        return None

    def _envelope(
        self, tool: Tool, arguments: Mapping[str, object], tick: int
    ) -> ActionEnvelope:
        request_id = request_id_for(self.name, self.role, tick, tool)
        return ActionEnvelope(
            version=ACTION_VERSION,
            episode_id=self.episode_id,
            actor_id=self.actor_id,
            actor_type="worker",
            role=self.role,
            tool=tool,
            arguments=dict(arguments),
            tick=tick,
            timestamp=self.epoch + timedelta(minutes=tick),
            request_id=request_id,
            idempotency_key=idempotency_key_for(self.episode_id, request_id),
        )

    def _require_role(self, observation: Observation) -> None:
        if observation.role != self.role:
            raise ValueError(
                f"{type(self).__name__} for role {self.role!r} was handed"
                + f" a {observation.role!r} observation"
            )


class IdlePolicy(_PolicyBase):
    """Emits nothing, ever.

    The floor for any graded comparison: whatever the episode does with no
    worker acting at all is the score a policy has to beat.
    """

    name: str = "idle"

    def decide(self, observation: Observation) -> ActionEnvelope | None:
        self._require_role(observation)
        return None


class GreedyPolicy(_PolicyBase):
    """Takes the highest-priority action its observation justifies.

    "Greedy" is per-role, because the roles see different things:

    ``chef``
        Prepare the highest-priority order not already sent to the pass and
        whose items the observed inventory covers; otherwise inspect the
        highest-priority order, else an equipment item.
    ``server``
        Serve the longest-waiting table whose order reads ``ready``, marking
        it verified when the guest constraints name an allergy for it.
    ``host``
        Seat the largest waiting party at the smallest table that fits it.
    ``expediter``
        Coordinate the chef on the highest-priority order still in play,
        escalating it to ``high``.

    Every branch checks the preconditions ``RestaurantSim.act`` validates, so
    a greedy envelope is always accepted -- that is the point of the policy as
    a test instrument. When no branch applies it returns ``None`` rather than
    emitting something the validator would reject.

    ``rng``, when supplied, breaks ties between equally-ranked candidates. It
    is the caller's seeded :class:`Rng`, drawn from in a fixed order, so a
    replay from the same seed makes the same draws. Without it ties fall to
    the total order on entity id.
    """

    name: str = "greedy"

    def __init__(
        self,
        *,
        episode_id: str,
        actor_id: str,
        role: Role,
        epoch: datetime = POLICY_EPOCH,
        rng: Rng | None = None,
    ) -> None:
        super().__init__(
            episode_id=episode_id, actor_id=actor_id, role=role, epoch=epoch
        )
        self._rng: Rng | None = rng
        # See the module docstring: the chef projection carries no status, so
        # the policy tracks its own prepare requests rather than re-issuing
        # them and collecting an ``invalid_state`` rejection every tick.
        self._prepare_requested: set[str] = set()

    @override
    def reset(self) -> None:
        self._prepare_requested = set()

    def decide(self, observation: Observation) -> ActionEnvelope | None:
        self._require_role(observation)
        if isinstance(observation, ChefObservation):
            return self._decide_chef(observation)
        if isinstance(observation, ServerObservation):
            return self._decide_server(observation)
        if isinstance(observation, HostObservation):
            return self._decide_host(observation)
        return self._decide_expediter(observation)

    # -- deterministic selection ------------------------------------------

    def _choose(
        self, items: Sequence[_T], key: Callable[[_T], tuple[object, ...]]
    ) -> _T | None:
        """The best item under ``key``, ties broken by ``rng`` or by ``key``.

        ``key`` returns a sort tuple whose first element is the rank; items
        sharing the best rank are the tie pool. ``key``'s remaining elements
        must make the order total (an entity id is always the last one), so
        the no-RNG path is deterministic on its own.
        """
        if not items:
            return None
        ranked = sorted(items, key=key)
        best_rank = key(ranked[0])[0]
        if self._rng is not None:
            tied = [item for item in ranked if key(item)[0] == best_rank]
            if len(tied) > 1:
                return tied[self._rng.int(len(tied))]
        return ranked[0]

    # -- per-role decisions ------------------------------------------------

    def _decide_chef(self, observation: ChefObservation) -> ActionEnvelope | None:
        startable = [
            order
            for order in observation.orders
            if order.id not in self._prepare_requested
            and _inventory_covers(observation.inventory, order.items)
        ]
        chosen = self._choose(
            startable, lambda o: (_PRIORITY_RANK[o.priority], o.id)
        )
        if chosen is not None:
            self._prepare_requested.add(chosen.id)
            return self._envelope("prepare", {"orderId": chosen.id}, observation.tick)

        # Nothing startable: look at something real rather than guess. inspect
        # only requires the target to exist, which the projection guarantees.
        target = self._choose(
            observation.orders, lambda o: (_PRIORITY_RANK[o.priority], o.id)
        )
        if target is not None:
            return self._envelope(
                "inspect", {"targetId": target.id}, observation.tick
            )
        equipment = self._choose(observation.equipment, lambda e: (e.id,))
        if equipment is not None:
            return self._envelope(
                "inspect", {"targetId": equipment.id}, observation.tick
            )
        return None

    def _decide_server(self, observation: ServerObservation) -> ActionEnvelope | None:
        servable = [
            table
            for table in observation.tables
            if table.status == _READY and table.order_id is not None
        ]
        # Longest wait first; the projection carries no priority for the
        # server, so wait time is the only ranking signal it is given.
        chosen = self._choose(servable, lambda t: (-t.wait_minutes, t.id))
        if chosen is None or chosen.order_id is None:
            return None
        allergic = {
            constraint.order_id
            for constraint in observation.guest_constraints
            if constraint.kind == "allergy"
        }
        return self._envelope(
            "serve",
            {"orderId": chosen.order_id, "verified": chosen.order_id in allergic},
            observation.tick,
        )

    def _decide_host(self, observation: HostObservation) -> ActionEnvelope | None:
        party = self._choose(observation.queue, lambda p: (-p.size, p.party_id))
        if party is None:
            return None
        # HostObservation.tables is already the empty-table projection, so the
        # only remaining seat() precondition is capacity.
        fitting = [table for table in observation.tables if table.seats >= party.size]
        table = self._choose(fitting, lambda t: (t.seats, t.id))
        if table is None:
            return None
        return self._envelope(
            "seat",
            {"tableId": table.id, "partySize": party.size},
            observation.tick,
        )

    def _decide_expediter(
        self, observation: ExpediterObservation
    ) -> ActionEnvelope | None:
        chefs = [worker for worker in observation.workers if worker.role == "chef"]
        pool = chefs or list(observation.workers)
        target_worker = self._choose(pool, lambda w: (w.load, w.id))
        if target_worker is None:
            return None
        order = self._choose(
            observation.orders, lambda o: (_PRIORITY_RANK[o.priority], o.id)
        )
        arguments: dict[str, object] = {"workerId": target_worker.id}
        if order is not None:
            arguments["orderId"] = order.id
            arguments["priority"] = "high"
        return self._envelope("coordinate", arguments, observation.tick)


def _inventory_covers(inventory: Mapping[str, int], items: Sequence[str]) -> bool:
    """Whether ``inventory`` holds every unit ``items`` asks for.

    Mirrors ``RestaurantSim._apply_prepare``'s per-item quantity check, so a
    greedy prepare never trips ``invalid_state`` on stock.
    """
    required: dict[str, int] = {}
    for item in items:
        required[item] = required.get(item, 0) + 1
    return all(inventory.get(item, 0) >= quantity for item, quantity in required.items())
