"""The manager half of the hybrid staff contract (#75).

The single-model harness in :mod:`retro_backend.esim.runner` has one model
answer as all four workers. This module adds the shape human managers play:
**one manager model directs four workers, and the workers are operated by a
different, fixed model.** Manager identity and worker identity are separate
configuration and separate fields in every record, so a ranking of managers
holds worker identity constant rather than mixing the two.

Three things are load-bearing.

*The manager never touches the restaurant.* Its tools write standing
directives and nothing else -- no order moves, no table is seated, no
equipment is touched by a manager action. Everything the manager achieves it
achieves through what the workers then choose to do, which is what makes the
manager the thing being measured.

*Directives are validated, not free text pasted into a prompt.* A manager
turn parses into a :class:`ManagerActionEnvelope`, is checked against the
per-tool argument models here, and is either applied to the
:class:`DirectiveBoard` or rejected with the same
:class:`~retro_backend.esim.contracts.ActionError` shape a worker gets. A
rejection is recorded and fed back as ``previousError``, exactly as on the
worker path.

*The manager sees a projection, like every other role.* It sees the shift it
is running -- staff, orders, equipment, visible incidents, and what its own
staff did last round. It does not see the event schedule, future incidents,
grader weights, probe definitions, or any score, and neither do the workers
it directs.
"""

from collections.abc import Mapping, Sequence
from typing import Literal

from pydantic import Field, ValidationError

from retro_backend.esim.contracts import (
    ActionError,
    ActionErrorCode,
    ArenaId,
    ArenaModel,
    EquipmentKind,
    EquipmentStatus,
    Load,
    OrderStatus,
    Priority,
    RestaurantState,
    Role,
    Tool,
)
from retro_backend.esim.state_hash import state_hash
from retro_backend.esim.tools import ToolArgs

MANAGER_ACTION_VERSION = "restaurant-arena-manager-action/v1"
MANAGER_OBSERVATION_VERSION = "restaurant-arena-manager-observation/v1"

# The manager is staff, not a player: it is recorded under its own actor id so
# no reader can mistake a manager decision for a worker's or for the scripted
# player correction the case schedule delivers.
MANAGER_ACTOR_ID = "manager"

# "all" is a broadcast slot, not a fifth role: it holds one directive that
# every worker sees in addition to its own role's.
DirectiveTarget = Literal["chef", "server", "host", "expediter", "all"]
DIRECTIVE_TARGETS: tuple[DirectiveTarget, ...] = (
    "chef",
    "server",
    "host",
    "expediter",
    "all",
)

ManagerTool = Literal["set_directive", "clear_directive"]

__all__ = [
    "DIRECTIVE_TARGETS",
    "MANAGER_ACTION_VERSION",
    "MANAGER_ACTOR_ID",
    "MANAGER_OBSERVATION_VERSION",
    "MANAGER_TOOL_ARGS",
    "ClearDirectiveArgs",
    "DirectiveBoard",
    "DirectiveTarget",
    "ManagerActionEnvelope",
    "ManagerObservation",
    "ManagerTool",
    "SetDirectiveArgs",
    "StandingDirective",
    "WorkerActivity",
    "observe_manager",
]


class SetDirectiveArgs(ToolArgs):
    """Post a standing instruction for one role, or for the whole floor.

    ``instruction`` is capped at the same 256 characters ``send_message``
    allows: a directive is read by a worker on every turn it stands, so a long
    one crowds out the observation it is supposed to be read against.
    """

    target_role: DirectiveTarget
    instruction: str = Field(min_length=1, max_length=256)
    priority: Priority = "normal"


class ClearDirectiveArgs(ToolArgs):
    """Withdraw the standing instruction currently posted for one target."""

    target_role: DirectiveTarget


MANAGER_TOOL_ARGS: dict[ManagerTool, type[ToolArgs]] = {
    "set_directive": SetDirectiveArgs,
    "clear_directive": ClearDirectiveArgs,
}


class StandingDirective(ArenaModel):
    """One instruction in force until the manager replaces or clears it."""

    target_role: DirectiveTarget
    instruction: str = Field(min_length=1, max_length=256)
    priority: Priority = "normal"
    issued_at_round: int = Field(ge=1, strict=True)
    issued_at_tick: int = Field(ge=0, strict=True)


class ManagerActionEnvelope(ArenaModel):
    """One manager turn's decision, before the board has judged it.

    Deliberately not an :class:`~retro_backend.esim.contracts.ActionEnvelope`:
    that envelope is the worker/player contract ``restaurant-arena-action/v1``
    pins, its ``role`` is one of the four worker roles, and the simulator
    applies it to restaurant state. A manager decision is none of those
    things, and widening the frozen envelope to carry it would let a manager
    action reach ``RestaurantSim.act``.
    """

    version: Literal["restaurant-arena-manager-action/v1"] = MANAGER_ACTION_VERSION
    episode_id: ArenaId
    actor_id: ArenaId = MANAGER_ACTOR_ID
    actor_type: Literal["manager"] = "manager"
    tool: ManagerTool
    arguments: Mapping[str, object]
    round: int = Field(ge=1, strict=True)
    tick: int = Field(ge=0, strict=True)
    request_id: ArenaId


def _reject(code: ActionErrorCode, message: str) -> ActionError:
    # Every rejection a manager can draw is a correctable request -- a bad
    # argument shape, or clearing a directive that is not posted -- so the
    # manager turn is allowed another attempt within its retry budget.
    return ActionError(code=code, message=message, retryable=True)


class DirectiveBoard:
    """The standing directives in force, and the only thing a manager writes.

    At most one directive stands per target, so a manager that keeps talking
    replaces its own instruction rather than burying the workers under a
    growing pile. That also makes "the instructions in force" a small, fully
    ordered object, which is what :meth:`digest` needs to be worth recording.
    """

    def __init__(self) -> None:
        self._by_target: dict[DirectiveTarget, StandingDirective] = {}

    def apply(self, envelope: ManagerActionEnvelope) -> StandingDirective | None | ActionError:
        """Validate one manager decision and apply it, or reject it.

        Returns the directive now standing for ``set_directive``, ``None`` for
        an accepted ``clear_directive``, and an
        :class:`~retro_backend.esim.contracts.ActionError` for anything the
        board refuses -- the same three-way answer the worker path gives, so a
        caller records both alike.
        """
        if envelope.tool == "set_directive":
            try:
                posted = SetDirectiveArgs.model_validate(envelope.arguments)
            except ValidationError as exc:
                return _reject("invalid_arguments", str(exc)[:256])
            directive = StandingDirective(
                target_role=posted.target_role,
                instruction=posted.instruction,
                priority=posted.priority,
                issued_at_round=envelope.round,
                issued_at_tick=envelope.tick,
            )
            self._by_target[posted.target_role] = directive
            return directive

        try:
            cleared = ClearDirectiveArgs.model_validate(envelope.arguments)
        except ValidationError as exc:
            return _reject("invalid_arguments", str(exc)[:256])
        if cleared.target_role not in self._by_target:
            return _reject(
                "invalid_state", f"no directive stands for {cleared.target_role}"
            )
        del self._by_target[cleared.target_role]
        return None

    def active(self) -> tuple[StandingDirective, ...]:
        """Every standing directive, in the fixed target order."""
        return tuple(
            self._by_target[target]
            for target in DIRECTIVE_TARGETS
            if target in self._by_target
        )

    def for_role(self, role: Role) -> tuple[StandingDirective, ...]:
        """What ``role`` is standing under: its own directive, then the floor's.

        Role-specific first so a worker reading top-down sees the instruction
        addressed to it before the broadcast one.
        """
        return tuple(
            self._by_target[target]
            for target in (role, "all")
            if target in self._by_target
        )

    def digest(self) -> str:
        """A ``sha256:`` digest of the instructions currently in force.

        Recorded beside every worker decision so a transcript says which
        manager instructions produced it. Two runs whose worker replies look
        alike but whose digests differ were taken under different management
        and are not interchangeable -- #75's "do not reuse a worker transcript
        after different manager instructions", enforced by evidence rather
        than by trust.
        """
        return state_hash(
            [
                directive.model_dump(by_alias=True, mode="json")
                for directive in self.active()
            ]
        )


class ObservedStaff(ArenaModel):
    """One worker as the manager sees it: who, doing what, how loaded."""

    id: ArenaId
    role: Role
    load: Load
    last_action: str | None = None


class ObservedShiftOrder(ArenaModel):
    """An order as the manager sees it: status and constraints, not contents."""

    id: ArenaId
    status: OrderStatus
    priority: Priority
    allergy: str | None = None


class ObservedShiftEquipment(ArenaModel):
    id: ArenaId
    kind: EquipmentKind
    status: EquipmentStatus


class WorkerActivity(ArenaModel):
    """What one worker did on the round that just finished.

    Reported from the run's own action log, not from anything hidden: the
    manager is told what its staff chose and whether the restaurant accepted
    it, which is what a manager on a real floor can see. ``tool`` is ``None``
    when the worker stood down or never produced a usable decision.
    """

    role: Role
    worker_id: ArenaId
    tool: Tool | None = None
    accepted: bool = False
    error_code: ActionErrorCode | None = None


class ManagerObservation(ArenaModel):
    """The shift as the manager sees it at the top of one round."""

    version: Literal["restaurant-arena-manager-observation/v1"] = (
        MANAGER_OBSERVATION_VERSION
    )
    role: Literal["manager"] = "manager"
    round: int = Field(ge=1, strict=True)
    rounds_total: int = Field(ge=1, strict=True)
    tick: int = Field(ge=0, strict=True)
    ticks_remaining: int = Field(ge=0, strict=True)
    staff: tuple[ObservedStaff, ...]
    orders: tuple[ObservedShiftOrder, ...]
    equipment: tuple[ObservedShiftEquipment, ...]
    emergency: bool
    standing_directives: tuple[StandingDirective, ...] = ()
    last_round: tuple[WorkerActivity, ...] = ()


def observe_manager(
    state: RestaurantState,
    *,
    round_number: int,
    rounds_total: int,
    directives: Sequence[StandingDirective] = (),
    last_round: Sequence[WorkerActivity] = (),
) -> ManagerObservation:
    """Project ``state`` into what the manager is allowed to see.

    A pure projection, like the four worker projections in
    :mod:`retro_backend.esim.observations`: no I/O, no RNG, no mutation, and
    nothing from outside ``state`` except the run's own bookkeeping (which
    round this is, what the manager itself posted, what its staff just did).

    What is deliberately absent: the event schedule and any future incident,
    ``Outcomes`` -- which carries the derived satisfaction, coordination, and
    correction-uptake figures the grader also reads -- and every grader weight
    and probe definition. The manager runs a shift; it does not see the exam.
    """
    return ManagerObservation(
        round=round_number,
        rounds_total=rounds_total,
        tick=state.episode.tick,
        ticks_remaining=max(0, state.episode.max_ticks - state.episode.tick),
        staff=tuple(
            ObservedStaff(
                id=worker.id,
                role=worker.role,
                load=worker.load,
                last_action=worker.last_action,
            )
            for worker in state.workers
        ),
        orders=tuple(
            ObservedShiftOrder(
                id=order.id,
                status=order.status,
                priority=order.priority,
                allergy=order.allergy,
            )
            for order in state.orders
        ),
        equipment=tuple(
            ObservedShiftEquipment(id=item.id, kind=item.kind, status=item.status)
            for item in state.equipment
        ),
        emergency=state.emergency is not None and state.emergency.active,
        standing_directives=tuple(directives),
        last_round=tuple(last_round),
    )
