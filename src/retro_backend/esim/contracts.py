"""Pure-Python contracts for the frozen Restaurant E-Sim.

These mirror ``restaurant-arena/v1`` as specified in tui-gamepigeon's
``docs/restaurant-arena-state-contract.md`` and ``docs/restaurant-arena-tools.md``.
Those files are untracked in that repo, so this module pins their *content*: the
published JSON Schemas in ``contracts/restaurant-arena-*-v1.schema.json`` are
authored here for tui-gamepigeon to back-port (#54).

The wire format is camelCase; Python attributes stay snake_case, so every dump
that leaves this process must pass ``by_alias=True``. Models are frozen and
forbid extra fields, matching ``retro_backend.contracts``.
"""

from collections.abc import Mapping
from typing import Annotated, Literal

from pydantic import AwareDatetime, BaseModel, ConfigDict, Field
from pydantic.alias_generators import to_camel

STATE_VERSION = "restaurant-arena/v1"
OBSERVATION_VERSION = "restaurant-arena-observation/v1"
ACTION_VERSION = "restaurant-arena-action/v1"

# Entity identifiers are authored in the frozen case, not generated per run, so
# they stay short and slug-shaped ("order-1", "worker-chef", "dinner-rush-001").
ARENA_ID_PATTERN = r"^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$"
ARENA_ID_MAX_LENGTH = 64
# Idempotency keys are "<episodeId>:<requestId>", so they also carry a colon.
IDEMPOTENCY_KEY_PATTERN = r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,129}$"
IDEMPOTENCY_KEY_MAX_LENGTH = 130

Role = Literal["chef", "server", "host", "expediter"]
OrderStatus = Literal[
    "queued",
    "seated",
    "preparing",
    "ready",
    "served",
    "cancelled",
]
Priority = Literal["normal", "high"]
TableStatus = Literal["empty", "occupied"]
EquipmentKind = Literal["oven", "warming_shelf"]
EquipmentStatus = Literal["working", "failed", "degraded"]
WorkerLocation = Literal["floor", "kitchen", "pass", "queue"]
EpisodePhase = Literal["active", "complete"]
ActorType = Literal["worker", "player"]

Tool = Literal[
    "inspect",
    "prepare",
    "serve",
    "seat",
    "update_guest",
    "coordinate",
    "request_help",
    "send_message",
]

# The contract names the shape of a rejection but never enumerates the codes.
# They live here because ActionError cannot be typed without them; #57 wires
# them into per-tool validation.
ActionErrorCode = Literal[
    "role_not_permitted",
    "invalid_arguments",
    "unknown_entity",
    "invalid_state",
    "stale_tick",
    "duplicate_request",
    "unsafe_action",
    "episode_complete",
]

ArenaId = Annotated[
    str,
    Field(pattern=ARENA_ID_PATTERN, max_length=ARENA_ID_MAX_LENGTH),
]
# Load is a 0..1 saturation figure, not a queue depth.
Load = Annotated[float, Field(ge=0.0, le=1.0)]
Rate = Annotated[float, Field(ge=0.0, le=1.0)]
Count = Annotated[int, Field(ge=0, strict=True)]


class ArenaModel(BaseModel):
    """Frozen, closed model that speaks camelCase on the wire."""

    model_config = ConfigDict(
        extra="forbid",
        frozen=True,
        populate_by_name=True,
        alias_generator=to_camel,
    )


class Episode(ArenaModel):
    id: ArenaId
    phase: EpisodePhase
    tick: int = Field(ge=0, strict=True)
    max_ticks: int = Field(ge=1, strict=True)


class Clock(ArenaModel):
    minute: int = Field(ge=0, strict=True)
    # Integral so a state's canonical JSON spelling stays stable; the demo
    # never runs at a fractional speed.
    speed: int = Field(ge=1, strict=True)


class Table(ArenaModel):
    id: ArenaId
    seats: int = Field(ge=1, strict=True)
    status: TableStatus
    order_id: ArenaId | None = None


class Order(ArenaModel):
    id: ArenaId
    table_id: ArenaId | None = None
    items: tuple[str, ...]
    status: OrderStatus
    allergy: str | None = None
    priority: Priority


class EquipmentItem(ArenaModel):
    id: ArenaId
    kind: EquipmentKind
    status: EquipmentStatus
    safety_threshold: float | None = None
    reading: float | None = None


class WorkerState(ArenaModel):
    id: ArenaId
    role: Role
    load: Load
    location: WorkerLocation
    last_action: str | None = None


class Emergency(ArenaModel):
    kind: Literal["oven_failure"]
    active: bool
    started_at: int = Field(ge=0, strict=True)


class Outcomes(ArenaModel):
    cash: float = Field(ge=0.0)
    satisfaction: Rate
    waste: float = Field(ge=0.0)
    wait_time: float = Field(ge=0.0)
    completed: Count
    safe_orders: Count
    coordination: Rate
    correction_uptake: Rate


class RestaurantState(ArenaModel):
    """The one authoritative state; UI, workers, replay, and graders read it."""

    version: Literal["restaurant-arena/v1"] = STATE_VERSION
    seed: int = Field(ge=0, strict=True)
    episode: Episode
    clock: Clock
    tables: tuple[Table, ...]
    orders: tuple[Order, ...]
    inventory: Mapping[str, int]
    equipment: tuple[EquipmentItem, ...]
    workers: tuple[WorkerState, ...]
    emergency: Emergency | None = None
    reputation: float = Field(ge=0.0, le=100.0)
    outcomes: Outcomes


class ObservedOrder(ArenaModel):
    """An order as the chef sees it."""

    id: ArenaId
    items: tuple[str, ...]
    allergy: str | None = None
    priority: Priority


class ObservedEquipment(ArenaModel):
    id: ArenaId
    status: EquipmentStatus


class ObservedTable(ArenaModel):
    """A seated table as the server sees it."""

    id: ArenaId
    order_id: ArenaId | None = None
    status: OrderStatus
    wait_minutes: int = Field(ge=0, strict=True)


class GuestConstraint(ArenaModel):
    order_id: ArenaId
    kind: Literal["allergy", "dietary", "timing"]
    value: str


class QueuedParty(ArenaModel):
    party_id: ArenaId
    size: int = Field(ge=1, strict=True)
    wait_minutes: int = Field(ge=0, strict=True)


class AvailableTable(ArenaModel):
    """A table as the host sees it: capacity, not order contents."""

    id: ArenaId
    seats: int = Field(ge=1, strict=True)
    status: TableStatus


class HandoffOrder(ArenaModel):
    id: ArenaId
    status: OrderStatus
    priority: Priority
    handoff: Literal["kitchen", "pass", "floor"]


class ObservedWorker(ArenaModel):
    id: ArenaId
    role: Role
    load: Load


class ObservedIncident(ArenaModel):
    kind: Literal["oven_failure", "warming_shelf_overheat"]
    active: bool


class ObservationBase(ArenaModel):
    version: Literal["restaurant-arena-observation/v1"] = OBSERVATION_VERSION
    tick: int = Field(ge=0, strict=True)
    load: Load


class ChefObservation(ObservationBase):
    role: Literal["chef"] = "chef"
    orders: tuple[ObservedOrder, ...]
    inventory: Mapping[str, int]
    equipment: tuple[ObservedEquipment, ...]


class ServerObservation(ObservationBase):
    role: Literal["server"] = "server"
    tables: tuple[ObservedTable, ...]
    guest_constraints: tuple[GuestConstraint, ...]


class HostObservation(ObservationBase):
    role: Literal["host"] = "host"
    queue: tuple[QueuedParty, ...]
    tables: tuple[AvailableTable, ...]
    wait_estimate_minutes: int = Field(ge=0, strict=True)


class ExpediterObservation(ObservationBase):
    role: Literal["expediter"] = "expediter"
    orders: tuple[HandoffOrder, ...]
    workers: tuple[ObservedWorker, ...]
    incidents: tuple[ObservedIncident, ...]


Observation = Annotated[
    ChefObservation | ServerObservation | HostObservation | ExpediterObservation,
    Field(discriminator="role"),
]


class ActionEnvelope(ArenaModel):
    """One worker action or typed player suggestion.

    ``actorType`` separates the two; the simulator applies role permissions to
    worker actions and never treats player text as a direct state mutation.
    """

    version: Literal["restaurant-arena-action/v1"] = ACTION_VERSION
    episode_id: ArenaId
    actor_id: ArenaId
    actor_type: ActorType
    role: Role
    tool: Tool
    # Per-tool argument schemas are #57; the envelope only promises an object.
    arguments: Mapping[str, object]
    tick: int = Field(ge=0, strict=True)
    timestamp: AwareDatetime
    request_id: ArenaId
    idempotency_key: str = Field(
        pattern=IDEMPOTENCY_KEY_PATTERN,
        max_length=IDEMPOTENCY_KEY_MAX_LENGTH,
    )


class ActionError(ArenaModel):
    """A stable rejection. The caller learns whether retrying can help."""

    code: ActionErrorCode
    message: str = Field(min_length=1, max_length=256)
    retryable: bool
