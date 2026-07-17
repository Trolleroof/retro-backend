"""Complete validation and flattening for interaction trace chunks v2."""

from __future__ import annotations

import json
from typing import Annotated, Any, Literal

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    StringConstraints,
    ValidationError,
    model_validator,
)

from .types import BrokerRecord, EventRow

UserId = Annotated[str, StringConstraints(pattern=r"^usr_[A-Za-z0-9._-]+$")]
GameId = Annotated[str, StringConstraints(pattern=r"^game_[A-Za-z0-9._-]+$")]
ChunkId = Annotated[str, StringConstraints(pattern=r"^chk_[A-Za-z0-9._-]+$")]
EventId = Annotated[str, StringConstraints(pattern=r"^evt_[A-Za-z0-9._-]+$")]


class RecordValidationError(ValueError):
    """A broker record cannot be materialized as a supported trace chunk."""


class TraceClient(BaseModel):
    model_config = ConfigDict(extra="forbid")

    platform: Literal["tui"]
    application_version: Annotated[str, StringConstraints(min_length=1)]
    recording_sdk_version: Annotated[str, StringConstraints(min_length=1)]


class TraceEvent(BaseModel):
    model_config = ConfigDict(extra="forbid")

    schema_version: Literal[2]
    user_id: UserId
    game_id: GameId
    chunk_id: ChunkId
    event_id: EventId
    event_sequence: int = Field(ge=1)
    event_type: Literal[
        "episode_started",
        "observation",
        "control_changed",
        "control_checkpoint",
        "outcome_changed",
        "engine_checkpoint",
        "episode_ended",
    ]
    event_version: Literal[1]
    visibility: Literal["policy", "privileged"]
    client_time_ms: float = Field(ge=0)
    monotonic_time_us: int = Field(ge=0)
    simulation_tick: int | None = Field(default=None, ge=0)
    payload: dict[str, Any]


class TraceChunkV2(BaseModel):
    model_config = ConfigDict(extra="forbid")

    schema_version: Literal[2]
    user_id: UserId
    game_id: GameId
    game_type: Annotated[str, StringConstraints(min_length=1)]
    game_version: Annotated[str, StringConstraints(min_length=1)]
    chunk_id: ChunkId
    chunk_sequence: int = Field(ge=1)
    first_event_sequence: int = Field(ge=1)
    last_event_sequence: int = Field(ge=1)
    event_count: int = Field(ge=1)
    created_at_ms: float = Field(ge=0)
    client: TraceClient
    events: list[TraceEvent] = Field(min_length=1)

    @model_validator(mode="after")
    def validate_event_envelope(self) -> TraceChunkV2:
        if self.event_count != len(self.events):
            raise ValueError("event_count must equal events length")
        sequences = [event.event_sequence for event in self.events]
        expected = list(range(self.first_event_sequence, self.last_event_sequence + 1))
        if sequences != expected:
            raise ValueError("event_sequence values must exactly match the chunk range")
        for event in self.events:
            if event.user_id != self.user_id:
                raise ValueError("event user_id must match chunk user_id")
            if event.game_id != self.game_id:
                raise ValueError("event game_id must match chunk game_id")
            if event.chunk_id != self.chunk_id:
                raise ValueError("event chunk_id must match chunk chunk_id")
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


def decode_v2_record(record: BrokerRecord) -> list[EventRow]:
    authenticated_user = _authenticated_user(record)
    try:
        chunk = TraceChunkV2.model_validate_json(record.value)
    except ValidationError as error:
        raise RecordValidationError(str(error)) from error
    if chunk.user_id != authenticated_user:
        raise RecordValidationError("embedded user_id must match the authenticated user")

    return [
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
            payload_json=json.dumps(event.payload, sort_keys=True, separators=(",", ":")),
            broker_partition=record.partition,
            broker_offset=record.offset,
            ingested_at_ms=record.timestamp_ms,
        )
        for event in chunk.events
    ]
