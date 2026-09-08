"""The recorded shape of one finished episode -- the grader's only input (#71).

``RestaurantSim`` hands its caller a new :class:`RestaurantState` and nothing
else, and :func:`~retro_backend.esim.runner.run_case` streamed those states to
a JSONL sink and kept only a summary. That left the deterministic grader with
no input: "score the completed trajectory and event log" needs a trajectory
and an event log to exist as objects. This module is that artifact.

Three design choices are load-bearing:

*One snapshot per tick, not per action.* A snapshot is the state at the
**start** of tick N, after the incidents scheduled for N have fired and before
any action taken on N. Per-action snapshots would grow with turn count and buy
nothing the action log does not already carry, and every probe here asks
either "what was true when the worker chose?" (the snapshot) or "what did the
worker choose?" (the log).

*Rejections are recorded.* An action the simulator refused is evidence about
the model, not noise -- an attempted unverified serve of an allergy order is
the clearest safety signal a run can produce, and it never reaches the state.

*Provenance travels with the data.* A grade is meaningless without the case,
seed, and contract versions it was produced against, so a trajectory that
outlives its process still names all four.

Nothing here calls a model or a provider. A trajectory round-trips through
JSON, so grading is an offline operation on a file.
"""

from collections.abc import Mapping, Sequence
from typing import Literal

from pydantic import Field

from retro_backend.esim.contracts import (
    ActionEnvelope,
    ActionError,
    ActionErrorCode,
    ArenaId,
    ArenaModel,
    IncidentKind,
    RestaurantState,
    Role,
    Tool,
)
from retro_backend.esim.simulator import ScheduledIncident
from retro_backend.esim.state_hash import state_hash

TRAJECTORY_VERSION = "restaurant-arena-trajectory/v1"

Termination = Literal["episode_complete", "turn_limit", "provider_invalid"]

__all__ = [
    "TRAJECTORY_VERSION",
    "ActionRecord",
    "IncidentRecord",
    "TickSnapshot",
    "Trajectory",
    "TrajectoryRecorder",
]


class ActionRecord(ArenaModel):
    """One action a worker asked for, and what the simulator did with it."""

    tick: int = Field(ge=0, strict=True)
    role: Role
    actor_id: ArenaId
    tool: Tool
    arguments: Mapping[str, object]
    request_id: ArenaId
    accepted: bool
    # Set on rejection only. The code, not the message: messages carry entity
    # ids and are not stable enough to grade against.
    error_code: ActionErrorCode | None = None


class IncidentRecord(ArenaModel):
    """One scheduled event that actually fired within the episode."""

    tick: int = Field(ge=1, strict=True)
    kind: IncidentKind
    target_id: ArenaId
    text: str | None = None


class TickSnapshot(ArenaModel):
    """The state at the start of ``tick``, before any action taken on it."""

    tick: int = Field(ge=0, strict=True)
    state: RestaurantState


class Trajectory(ArenaModel):
    """One completed episode, everything a grader is allowed to read."""

    version: Literal["restaurant-arena-trajectory/v1"] = TRAJECTORY_VERSION
    case_id: str = Field(min_length=1, max_length=64)
    env_version: str
    contract_version: str
    seed: int = Field(ge=0, strict=True)
    snapshots: tuple[TickSnapshot, ...] = Field(min_length=1)
    actions: tuple[ActionRecord, ...] = ()
    incidents: tuple[IncidentRecord, ...] = ()
    termination: Termination

    @property
    def initial_state(self) -> RestaurantState:
        return self.snapshots[0].state

    @property
    def final_state(self) -> RestaurantState:
        return self.snapshots[-1].state

    @property
    def final_tick(self) -> int:
        return self.snapshots[-1].tick

    @property
    def final_state_hash(self) -> str:
        return state_hash(self.final_state.model_dump(by_alias=True))

    def state_at(self, tick: int) -> RestaurantState | None:
        """The state at the start of ``tick``, or ``None`` if never reached."""
        for snapshot in self.snapshots:
            if snapshot.tick == tick:
                return snapshot.state
        return None

    def accepted_actions(self) -> tuple[ActionRecord, ...]:
        return tuple(record for record in self.actions if record.accepted)

    def incident_at(self, kind: IncidentKind) -> IncidentRecord | None:
        """The first incident of ``kind`` that fired, if any did."""
        return next((event for event in self.incidents if event.kind == kind), None)


class TrajectoryRecorder:
    """Accumulates one episode's evidence as a driver runs it.

    The driver stays in charge of the loop: this only watches. Call
    :meth:`record_state` for the state each tick opens with,
    :meth:`record_action` for every ``act()`` result including rejections,
    and :meth:`finish` once the episode ends.

    Incidents are derived from the frozen schedule rather than reported by
    the simulator: the schedule is the authority on what fires and when, and
    deriving keeps the simulator free of grader-facing instrumentation.
    """

    def __init__(
        self,
        *,
        case_id: str,
        seed: int,
        env_version: str,
        contract_version: str,
        event_schedule: Sequence[ScheduledIncident] = (),
    ) -> None:
        self._case_id = case_id
        self._seed = seed
        self._env_version = env_version
        self._contract_version = contract_version
        self._event_schedule = tuple(event_schedule)
        self._snapshots: list[TickSnapshot] = []
        self._actions: list[ActionRecord] = []

    def record_state(self, state: RestaurantState) -> None:
        """Snapshot the state opening a tick.

        Re-recording the same tick replaces the earlier snapshot, so a driver
        that calls this both after ``reset`` and at the top of its loop does
        not double-count tick 0.
        """
        snapshot = TickSnapshot(tick=state.episode.tick, state=state)
        if self._snapshots and self._snapshots[-1].tick == snapshot.tick:
            self._snapshots[-1] = snapshot
            return
        self._snapshots.append(snapshot)

    def record_action(
        self, envelope: ActionEnvelope, result: RestaurantState | ActionError
    ) -> None:
        rejected = isinstance(result, ActionError)
        self._actions.append(
            ActionRecord(
                tick=envelope.tick,
                role=envelope.role,
                actor_id=envelope.actor_id,
                tool=envelope.tool,
                arguments=dict(envelope.arguments),
                request_id=envelope.request_id,
                accepted=not rejected,
                error_code=result.code if isinstance(result, ActionError) else None,
            )
        )

    def finish(self, termination: Termination = "episode_complete") -> Trajectory:
        if not self._snapshots:
            raise ValueError("cannot finish a trajectory with no recorded state")
        reached = self._snapshots[-1].tick
        incidents = tuple(
            IncidentRecord(
                tick=incident.tick,
                kind=incident.kind,
                target_id=incident.target_id,
                text=incident.text,
            )
            for incident in sorted(self._event_schedule, key=lambda i: (i.tick, i.kind))
            if incident.tick <= reached
        )
        return Trajectory(
            case_id=self._case_id,
            env_version=self._env_version,
            contract_version=self._contract_version,
            seed=self._seed,
            snapshots=tuple(self._snapshots),
            actions=tuple(self._actions),
            incidents=incidents,
            termination=termination,
        )
