"""Pure-Python request contracts shared by Worker HTTP and Queue handlers."""

import json
import re
from collections.abc import Mapping
from typing import Literal

from pydantic import (
    AwareDatetime,
    BaseModel,
    ConfigDict,
    Field,
    ValidationError,
    model_validator,
)

from .bindings import is_binding_null, value_field

GameType = Literal[
    "2048",
    "tetris",
    "snake",
    "minesweeper",
    "sokoban",
    "connect4",
    "lightsout",
    "sudoku",
]


class ContractError(ValueError):
    """Raised when a valid envelope violates an authenticated contract."""


class EventRange(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    first: int = Field(ge=1)
    last: int = Field(ge=1)


class GameplayPointer(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    version: Literal[1] = 1
    r2_key: str
    payload_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    user_id: str
    game_id: str
    chunk_id: str
    received_at_ms: int = Field(ge=0)


class IngestAcknowledgment(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    accepted: Literal[True] = True
    duplicate: bool
    game_id: str
    chunk_id: str
    chunk_sequence: int = Field(ge=1)
    accepted_event_range: EventRange
    queued: Literal[True] = True
    server_received_at_ms: int = Field(ge=0)


class ChunkReservation(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    status: Literal["reserved", "duplicate", "conflict", "publishing"]
    ack: IngestAcknowledgment | None = None

    @model_validator(mode="after")
    def validate_ack_status(self) -> "ChunkReservation":
        if self.status == "duplicate" and self.ack is None:
            raise ValueError("duplicate reservation requires acknowledgment")
        if self.status != "duplicate" and self.ack is not None:
            raise ValueError("only duplicate reservation may include acknowledgment")
        return self

    @classmethod
    def from_binding(cls, value: object) -> "ChunkReservation":
        """Normalize a structured-clone mapping or Pyodide proxy response."""
        status = value_field(value, "status")
        ack_value = value_field(value, "ack")
        data: dict[str, object] = {"status": status}
        if not is_binding_null(ack_value):
            data["ack"] = _acknowledgment_from_binding(ack_value)
        try:
            return cls.model_validate(data)
        except ValidationError as error:
            raise ContractError("invalid chunk reservation") from error


def _acknowledgment_from_binding(value: object) -> IngestAcknowledgment:
    if isinstance(value, IngestAcknowledgment):
        return value
    event_range = value_field(value, "accepted_event_range")
    data = {
        "accepted": value_field(value, "accepted"),
        "duplicate": value_field(value, "duplicate"),
        "game_id": value_field(value, "game_id"),
        "chunk_id": value_field(value, "chunk_id"),
        "chunk_sequence": value_field(value, "chunk_sequence"),
        "accepted_event_range": {
            "first": value_field(event_range, "first"),
            "last": value_field(event_range, "last"),
        },
        "queued": value_field(value, "queued"),
        "server_received_at_ms": value_field(value, "server_received_at_ms"),
    }
    return IngestAcknowledgment.model_validate(data)


class GameResultRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    result_id: str = Field(pattern=r"^res_[A-Za-z0-9._-]+$")
    game_id: str = Field(pattern=r"^game_[A-Za-z0-9._-]+$")
    game_type: GameType
    score: int = Field(ge=0, le=2**63 - 1, strict=True)
    completed_at: AwareDatetime


class ChunkEnvelope(BaseModel):
    model_config = ConfigDict(extra="allow")

    schema_version: Literal[2]
    user_id: str = Field(pattern=r"^usr_[A-Za-z0-9._-]+$")
    game_id: str = Field(pattern=r"^game_[A-Za-z0-9._-]+$")
    game_type: str = Field(min_length=1)
    game_version: str = Field(min_length=1)
    chunk_id: str = Field(pattern=r"^chk_[A-Za-z0-9._-]+$")
    chunk_sequence: int = Field(ge=1)
    first_event_sequence: int = Field(ge=1)
    last_event_sequence: int = Field(ge=1)
    event_count: int = Field(ge=1)
    created_at_ms: int = Field(ge=0)
    client: dict[str, object]
    events: list[dict[str, object]]

    @model_validator(mode="after")
    def validate_event_envelope(self) -> "ChunkEnvelope":
        if self.event_count != len(self.events):
            raise ValueError("event_count must equal events length")
        sequences: list[int] = []
        for index, event in enumerate(self.events):
            event_id = event.get("event_id")
            if not isinstance(event_id, str) or re.fullmatch(
                r"evt_[A-Za-z0-9._-]+", event_id
            ) is None:
                raise ValueError(f"events[{index}].event_id has an invalid format")
            sequence = event.get("event_sequence")
            if isinstance(sequence, bool) or not isinstance(sequence, int) or sequence < 1:
                raise ValueError(
                    f"events[{index}].event_sequence must be an integer >= 1"
                )
            sequences.append(sequence)
            for field, expected in (
                ("game_id", self.game_id),
                ("chunk_id", self.chunk_id),
            ):
                if event.get(field) != expected:
                    raise ValueError(f"events[{index}].{field} must match the chunk")
        if self.events[0].get("event_sequence") != self.first_event_sequence:
            raise ValueError("first_event_sequence must match the first event")
        if self.events[-1].get("event_sequence") != self.last_event_sequence:
            raise ValueError("last_event_sequence must match the last event")
        if any(
            current <= previous
            for previous, current in zip(sequences, sequences[1:], strict=False)
        ):
            raise ValueError("event sequences must be strictly increasing")
        return self

    def validate_authenticated_identity(self, user_id: str) -> None:
        if self.user_id != user_id:
            raise ContractError("user_id must match the authenticated user")
        for index, event in enumerate(self.events):
            if event.get("user_id") != user_id:
                raise ContractError(
                    f"events[{index}].user_id must match the authenticated user"
                )


def _reject_non_json_constant(value: str) -> None:
    raise ValueError(f"unsupported JSON constant: {value}")


def parse_chunk_envelope(raw_body: bytes, user_id: str) -> ChunkEnvelope:
    """Parse one UTF-8 JSON envelope, validate v2, then enforce auth identity."""
    try:
        payload = json.loads(
            raw_body.decode("utf-8"),
            parse_constant=_reject_non_json_constant,
        )
        if not isinstance(payload, Mapping):
            raise ValueError("chunk envelope must be an object")
        envelope = ChunkEnvelope.model_validate(payload)
    except (
        UnicodeDecodeError,
        json.JSONDecodeError,
        ValidationError,
        ValueError,
        RecursionError,
    ) as error:
        raise ContractError("invalid chunk envelope") from error

    envelope.validate_authenticated_identity(user_id)
    return envelope
