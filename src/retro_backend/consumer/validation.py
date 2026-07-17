"""Complete validation and flattening for interaction trace chunks v2."""

from __future__ import annotations

import json
import math
from typing import Annotated, Any, Literal, TypeAlias

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    StringConstraints,
    ValidationError,
    model_validator,
)

from .types import BrokerRecord, EventRow

INT32_MAX = 2**31 - 1
INT64_MAX = 2**63 - 1

NonEmptyString = Annotated[str, StringConstraints(min_length=1)]
UserId = Annotated[str, StringConstraints(pattern=r"^usr_[A-Za-z0-9._-]+$")]
GameId = Annotated[str, StringConstraints(pattern=r"^game_[A-Za-z0-9._-]+$")]
ChunkId = Annotated[str, StringConstraints(pattern=r"^chk_[A-Za-z0-9._-]+$")]
EventId = Annotated[str, StringConstraints(pattern=r"^evt_[A-Za-z0-9._-]+$")]
PositiveInt64 = Annotated[int, Field(strict=True, ge=1, le=INT64_MAX)]
NonNegativeInt64 = Annotated[int, Field(strict=True, ge=0, le=INT64_MAX)]
FiniteNumber = Annotated[float, Field(strict=True, allow_inf_nan=False)]
NonNegativeFiniteNumber = Annotated[float, Field(strict=True, ge=0, allow_inf_nan=False)]


class RecordValidationError(ValueError):
    """A broker record cannot be materialized as a supported trace chunk."""


class StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)


class TraceClient(StrictModel):
    platform: Literal["tui"]
    application_version: NonEmptyString
    recording_sdk_version: NonEmptyString


class Viewport(StrictModel):
    width: NonNegativeInt64
    height: NonNegativeInt64


class EpisodeStartedPayload(StrictModel):
    game_type: NonEmptyString
    game_version: NonEmptyString
    ruleset_id: NonEmptyString
    telemetry_version: Literal[2]
    platform: Literal["tui"]
    viewport: Viewport
    control_schema: list[NonEmptyString]
    input_semantics: Literal["terminal_impulse"]


class ObservationCaptureTiming(StrictModel):
    generated_time_us: NonNegativeInt64
    delivered_time_us: NonNegativeInt64
    dropped_observations: NonNegativeInt64


TerminalColor: TypeAlias = FiniteNumber | str | None


class TerminalCell(StrictModel):
    glyph: str
    foreground: TerminalColor
    background: TerminalColor
    attributes: NonNegativeInt64
    width: NonNegativeInt64


class PositionedTerminalCell(TerminalCell):
    x: NonNegativeInt64
    y: NonNegativeInt64


class TerminalCellChange(StrictModel):
    x: NonNegativeInt64
    y: NonNegativeInt64
    cell: TerminalCell | None


class FullTerminalObservation(StrictModel):
    observation_id: NonEmptyString
    representation: Literal["terminal_grid_v1"]
    width: NonNegativeInt64
    height: NonNegativeInt64
    state_hash: NonEmptyString
    cells: list[PositionedTerminalCell]
    capture_timing: ObservationCaptureTiming | None = None
    caused_by_event_id: EventId | None = None


class DeltaTerminalObservation(StrictModel):
    observation_id: NonEmptyString
    representation: Literal["terminal_grid_delta_v1"]
    base_observation_id: NonEmptyString
    width: NonNegativeInt64
    height: NonNegativeInt64
    state_hash: NonEmptyString
    changed_cells: list[TerminalCellChange]
    capture_timing: ObservationCaptureTiming | None = None
    caused_by_event_id: EventId | None = None


TerminalObservation = Annotated[
    FullTerminalObservation | DeltaTerminalObservation,
    Field(discriminator="representation"),
]


class ControlChangedPayload(StrictModel):
    based_on_observation_id: str | None
    control: NonEmptyString
    action: NonEmptyString
    kind: Literal["impulse"]
    actor: Literal["human", "model", "heuristic", "random", "environment"]
    action_received_us: NonNegativeInt64 | None = None


class ControlCheckpointPayload(StrictModel):
    held_controls: list[NonEmptyString]
    input_semantics: Literal["terminal_impulse"]


ProgressValue: TypeAlias = FiniteNumber | str | bool | None


class OutcomePerformance(StrictModel):
    action_received_us: NonNegativeInt64
    action_applied_us: NonNegativeInt64
    action_completed_us: NonNegativeInt64
    queueing_delay_us: NonNegativeInt64
    simulation_tick_duration_us: NonNegativeInt64


class OutcomeChangedPayload(StrictModel):
    score_before: FiniteNumber
    score_after: FiniteNumber
    score_delta: FiniteNumber
    engine_reward: FiniteNumber
    reward_spec_id: Literal["engine_v1"]
    terminal: bool
    caused_by_event_id: EventId
    progress: dict[str, ProgressValue] | None = None
    performance: OutcomePerformance | None = None


class EngineCheckpointPayload(StrictModel):
    state_version: Literal[1]
    reason: Literal["episode_start", "periodic", "random_outcome", "terminal", "episode_end"]
    state: Any
    state_hash: NonEmptyString
    seed: FiniteNumber | None = None
    random_effects: Any = None
    caused_by_event_id: EventId | None = None


class EpisodeEndedPayload(StrictModel):
    reason: Literal[
        "game_over",
        "won",
        "lost",
        "user_quit",
        "restart",
        "timeout",
        "client_closed",
        "engine_error",
    ]
    final_event_sequence: PositiveInt64
    final_score: FiniteNumber
    duration_us: NonNegativeInt64
    completed: bool


class TraceEventBase(StrictModel):
    schema_version: Literal[2]
    user_id: UserId
    game_id: GameId
    chunk_id: ChunkId
    event_id: EventId
    event_sequence: PositiveInt64
    event_version: Literal[1]
    client_time_ms: NonNegativeFiniteNumber
    monotonic_time_us: NonNegativeInt64
    simulation_tick: NonNegativeInt64 | None = None


class EpisodeStartedEvent(TraceEventBase):
    event_type: Literal["episode_started"]
    visibility: Literal["policy"]
    payload: EpisodeStartedPayload


class ObservationEvent(TraceEventBase):
    event_type: Literal["observation"]
    visibility: Literal["policy"]
    payload: TerminalObservation


class ControlChangedEvent(TraceEventBase):
    event_type: Literal["control_changed"]
    visibility: Literal["policy"]
    payload: ControlChangedPayload


class ControlCheckpointEvent(TraceEventBase):
    event_type: Literal["control_checkpoint"]
    visibility: Literal["policy"]
    payload: ControlCheckpointPayload


class OutcomeChangedEvent(TraceEventBase):
    event_type: Literal["outcome_changed"]
    visibility: Literal["policy"]
    payload: OutcomeChangedPayload


class EngineCheckpointEvent(TraceEventBase):
    event_type: Literal["engine_checkpoint"]
    visibility: Literal["privileged"]
    payload: EngineCheckpointPayload


class EpisodeEndedEvent(TraceEventBase):
    event_type: Literal["episode_ended"]
    visibility: Literal["policy"]
    payload: EpisodeEndedPayload

    @model_validator(mode="after")
    def validate_final_sequence(self) -> EpisodeEndedEvent:
        if self.payload.final_event_sequence != self.event_sequence:
            raise ValueError("final_event_sequence must equal event_sequence")
        return self


TraceEvent = Annotated[
    EpisodeStartedEvent
    | ObservationEvent
    | ControlChangedEvent
    | ControlCheckpointEvent
    | OutcomeChangedEvent
    | EngineCheckpointEvent
    | EpisodeEndedEvent,
    Field(discriminator="event_type"),
]


class TraceChunkV2(StrictModel):
    schema_version: Literal[2]
    user_id: UserId
    game_id: GameId
    game_type: NonEmptyString
    game_version: NonEmptyString
    chunk_id: ChunkId
    chunk_sequence: PositiveInt64
    first_event_sequence: PositiveInt64
    last_event_sequence: PositiveInt64
    event_count: PositiveInt64
    created_at_ms: NonNegativeFiniteNumber
    client: TraceClient
    events: list[TraceEvent] = Field(min_length=1)

    @model_validator(mode="after")
    def validate_event_envelope(self) -> TraceChunkV2:
        if self.event_count != len(self.events):
            raise ValueError("event_count must equal events length")
        if self.last_event_sequence < self.first_event_sequence:
            raise ValueError("last_event_sequence must not precede first_event_sequence")
        if self.last_event_sequence - self.first_event_sequence + 1 != len(self.events):
            raise ValueError("event sequence range must equal events length")
        for index, event in enumerate(self.events):
            if event.event_sequence != self.first_event_sequence + index:
                raise ValueError("event_sequence values must exactly match the chunk range")
            if event.user_id != self.user_id:
                raise ValueError("event user_id must match chunk user_id")
            if event.game_id != self.game_id:
                raise ValueError("event game_id must match chunk game_id")
            if event.chunk_id != self.chunk_id:
                raise ValueError("event chunk_id must match chunk chunk_id")
            if isinstance(event, EpisodeStartedEvent):
                if event.payload.game_type != self.game_type:
                    raise ValueError("episode_started game_type must match chunk game_type")
                if event.payload.game_version != self.game_version:
                    raise ValueError("episode_started game_version must match chunk game_version")
        return self


def _authenticated_user(record: BrokerRecord) -> str:
    values = [value for name, value in record.headers if name == "retro-user-id"]
    if len(values) != 1:
        raise RecordValidationError("exactly one retro-user-id header is required")
    try:
        user_id = values[0].decode("utf-8")
    except UnicodeDecodeError as error:
        raise RecordValidationError("retro-user-id must be valid UTF-8") from error
    if not user_id:
        raise RecordValidationError("retro-user-id must not be empty")
    return user_id


def _validate_broker_coordinates(record: BrokerRecord) -> None:
    coordinates = (
        ("partition", record.partition, INT32_MAX),
        ("offset", record.offset, INT64_MAX),
        ("timestamp_ms", record.timestamp_ms, INT64_MAX),
    )
    for name, value, maximum in coordinates:
        if type(value) is not int or not 0 <= value <= maximum:
            raise RecordValidationError(f"{name} must be a non-negative fixed-width integer")


def decode_v2_record(record: BrokerRecord) -> list[EventRow]:
    _validate_broker_coordinates(record)
    authenticated_user = _authenticated_user(record)
    try:
        chunk = TraceChunkV2.model_validate_json(record.value)
    except (ValidationError, ValueError) as error:
        raise RecordValidationError(str(error)) from error
    if chunk.user_id != authenticated_user:
        raise RecordValidationError("embedded user_id must match the authenticated user")

    rows: list[EventRow] = []
    try:
        for event in chunk.events:
            payload = event.payload.model_dump(mode="json")
            payload_json = json.dumps(
                payload,
                sort_keys=True,
                separators=(",", ":"),
                allow_nan=False,
            )
            if not math.isfinite(event.client_time_ms):
                raise ValueError("client_time_ms must be finite")
            rows.append(
                EventRow(
                    schema_version=event.schema_version,
                    user_id=authenticated_user,
                    game_id=chunk.game_id,
                    game_type=chunk.game_type,
                    game_version=chunk.game_version,
                    chunk_id=chunk.chunk_id,
                    chunk_sequence=chunk.chunk_sequence,
                    event_id=event.event_id,
                    event_sequence=event.event_sequence,
                    event_type=event.event_type,
                    event_version=event.event_version,
                    visibility=event.visibility,
                    client_time_ms=event.client_time_ms,
                    monotonic_time_us=event.monotonic_time_us,
                    simulation_tick=event.simulation_tick,
                    payload_json=payload_json,
                    broker_partition=record.partition,
                    broker_offset=record.offset,
                    ingested_at_ms=record.timestamp_ms,
                )
            )
    except (TypeError, ValueError, OverflowError) as error:
        raise RecordValidationError(f"event payload is not JSON-safe: {error}") from error
    return rows
