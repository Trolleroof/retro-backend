"""Pure-Python request contracts shared by Worker HTTP and Queue handlers."""

import json
import math
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


class PermanentGameplayError(RuntimeError):
    """A safe, deterministic reason that must be persisted before acknowledgment."""

    def __init__(self, reason_code: str, reason: str) -> None:
        super().__init__(reason)
        self.reason_code = reason_code
        self.reason = reason


class TransientGameplayError(RuntimeError):
    """A retryable gameplay consumer infrastructure failure."""


class GameplayProcessResult(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    status: Literal["processed", "rejected"]
    artifact_key: str
    reason_code: str | None = None

    @model_validator(mode="after")
    def validate_reason(self) -> "GameplayProcessResult":
        if self.status == "processed" and self.reason_code is not None:
            raise ValueError("processed result cannot include a reason code")
        if self.status == "rejected" and self.reason_code is None:
            raise ValueError("rejected result requires a reason code")
        return self


class EventRange(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    first: int = Field(ge=1)
    last: int = Field(ge=1)


class GameplayPointer(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    version: Literal[1] = 1
    r2_key: str = Field(min_length=1)
    payload_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    user_id: str = Field(pattern=r"^usr_[A-Za-z0-9._-]+$")
    game_id: str = Field(pattern=r"^game_[A-Za-z0-9._-]+$")
    chunk_id: str = Field(pattern=r"^chk_[A-Za-z0-9._-]+$")
    received_at_ms: int = Field(ge=0, strict=True)

    @model_validator(mode="after")
    def validate_raw_key(self) -> "GameplayPointer":
        expected = (
            f"raw/user={self.user_id}/game={self.game_id}/chunk={self.chunk_id}.json"
        )
        if self.r2_key != expected:
            raise ValueError("r2_key must match pointer identity")
        return self


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
    lease_token: str | None = Field(
        default=None,
        pattern=r"^[0-9a-f]{64}$",
    )
    ack: IngestAcknowledgment | None = None

    @model_validator(mode="after")
    def validate_status_fields(self) -> "ChunkReservation":
        if self.status == "reserved" and self.lease_token is None:
            raise ValueError("reserved reservation requires lease token")
        if self.status != "reserved" and self.lease_token is not None:
            raise ValueError("only reserved reservation may include lease token")
        if self.status == "duplicate" and self.ack is None:
            raise ValueError("duplicate reservation requires acknowledgment")
        if self.status != "duplicate" and self.ack is not None:
            raise ValueError("only duplicate reservation may include acknowledgment")
        return self

    @classmethod
    def from_binding(cls, value: object) -> "ChunkReservation":
        """Normalize a structured-clone mapping or Pyodide proxy response."""
        status = value_field(value, "status")
        lease_token = value_field(value, "lease_token")
        ack_value = value_field(value, "ack")
        data: dict[str, object] = {"status": status}
        if not is_binding_null(lease_token):
            data["lease_token"] = lease_token
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
    chunk_sequence: int = Field(ge=1, strict=True)
    first_event_sequence: int = Field(ge=1, strict=True)
    last_event_sequence: int = Field(ge=1, strict=True)
    event_count: int = Field(ge=1, strict=True)
    created_at_ms: int = Field(ge=0, strict=True)
    client: dict[str, object]
    events: list[dict[str, object]]

    @model_validator(mode="after")
    def validate_event_envelope(self) -> "ChunkEnvelope":
        expected_client_fields = {
            "platform",
            "application_version",
            "recording_sdk_version",
        }
        if set(self.client) != expected_client_fields:
            raise ValueError("client must contain exactly the v2 client fields")
        if self.client.get("platform") != "tui":
            raise ValueError("client.platform must be tui")
        for field in ("application_version", "recording_sdk_version"):
            value = self.client.get(field)
            if not isinstance(value, str) or not value:
                raise ValueError(f"client.{field} must be a non-empty string")

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
            _validate_v2_event(event, index)
        if self.events[0].get("event_sequence") != self.first_event_sequence:
            raise ValueError("first_event_sequence must match the first event")
        if self.events[-1].get("event_sequence") != self.last_event_sequence:
            raise ValueError("last_event_sequence must match the last event")
        if any(
            current <= previous
            for previous, current in zip(sequences, sequences[1:], strict=False)
        ):
            raise ValueError("event sequences must be strictly increasing")
        first_event = self.events[0]
        if first_event.get("event_type") != "episode_started":
            raise ValueError("the first event must be episode_started")
        start_payload = first_event.get("payload")
        if not isinstance(start_payload, Mapping):
            raise ValueError("the episode_started payload must be an object")
        if start_payload.get("game_type") != self.game_type:
            raise ValueError("episode_started game_type must match the chunk")
        if start_payload.get("game_version") != self.game_version:
            raise ValueError("episode_started game_version must match the chunk")
        return self

    def validate_authenticated_identity(self, user_id: str) -> None:
        if self.user_id != user_id:
            raise ContractError("user_id must match the authenticated user")
        for index, event in enumerate(self.events):
            if event.get("user_id") != user_id:
                raise ContractError(
                    f"events[{index}].user_id must match the authenticated user"
                )


_REQUIRED_EVENT_FIELDS = frozenset(
    {
        "schema_version",
        "user_id",
        "game_id",
        "chunk_id",
        "event_id",
        "event_sequence",
        "event_type",
        "event_version",
        "visibility",
        "client_time_ms",
        "monotonic_time_us",
        "payload",
    }
)
_ALLOWED_EVENT_FIELDS = _REQUIRED_EVENT_FIELDS | {"simulation_tick"}
_SUPPORTED_EVENT_TYPES = frozenset(
    {
        "episode_started",
        "observation",
        "control_changed",
        "control_checkpoint",
        "outcome_changed",
        "engine_checkpoint",
        "episode_ended",
    }
)
_EVENT_VISIBILITIES = frozenset({"policy", "privileged"})


def _is_nonnegative_number(value: object) -> bool:
    if isinstance(value, bool):
        return False
    if isinstance(value, int):
        return value >= 0
    return isinstance(value, float) and math.isfinite(value) and value >= 0


def _is_nonnegative_integer(value: object) -> bool:
    return not isinstance(value, bool) and isinstance(value, int) and value >= 0


def _validate_identifier(value: object, pattern: str) -> bool:
    return isinstance(value, str) and re.fullmatch(pattern, value) is not None


def _validate_v2_event(event: Mapping[str, object], index: int) -> None:
    fields = set(event)
    missing = _REQUIRED_EVENT_FIELDS - fields
    if missing:
        raise ValueError(f"events[{index}] is missing required v2 fields")
    if fields - _ALLOWED_EVENT_FIELDS:
        raise ValueError(f"events[{index}] contains unsupported v2 fields")
    if event.get("schema_version") != 2 or isinstance(
        event.get("schema_version"), bool
    ):
        raise ValueError(f"events[{index}].schema_version must equal 2")
    for field, pattern in (
        ("user_id", r"usr_[A-Za-z0-9._-]+"),
        ("game_id", r"game_[A-Za-z0-9._-]+"),
        ("chunk_id", r"chk_[A-Za-z0-9._-]+"),
        ("event_id", r"evt_[A-Za-z0-9._-]+"),
    ):
        if not _validate_identifier(event.get(field), pattern):
            raise ValueError(f"events[{index}].{field} has an invalid format")
    sequence = event.get("event_sequence")
    if isinstance(sequence, bool) or not isinstance(sequence, int) or sequence < 1:
        raise ValueError(f"events[{index}].event_sequence must be an integer >= 1")
    event_type = event.get("event_type")
    if not isinstance(event_type, str) or event_type not in _SUPPORTED_EVENT_TYPES:
        raise ValueError(f"events[{index}].event_type is unsupported")
    if event.get("event_version") != 1 or isinstance(
        event.get("event_version"), bool
    ):
        raise ValueError(f"events[{index}].event_version must equal 1")
    visibility = event.get("visibility")
    if not isinstance(visibility, str) or visibility not in _EVENT_VISIBILITIES:
        raise ValueError(f"events[{index}].visibility is unsupported")
    if not _is_nonnegative_number(event.get("client_time_ms")):
        raise ValueError(f"events[{index}].client_time_ms must be nonnegative")
    if not _is_nonnegative_integer(event.get("monotonic_time_us")):
        raise ValueError(
            f"events[{index}].monotonic_time_us must be a nonnegative integer"
        )
    if "simulation_tick" in event and not _is_nonnegative_integer(
        event.get("simulation_tick")
    ):
        raise ValueError(
            f"events[{index}].simulation_tick must be a nonnegative integer"
        )
    if not isinstance(event.get("payload"), Mapping):
        raise ValueError(f"events[{index}].payload must be an object")


def _reject_non_json_constant(value: str) -> None:
    raise ValueError(f"unsupported JSON constant: {value}")


def _load_chunk_payload(raw_body: bytes) -> object:
    return json.loads(
        raw_body.decode("utf-8"),
        parse_constant=_reject_non_json_constant,
    )


def parse_chunk_envelope(raw_body: bytes, user_id: str) -> ChunkEnvelope:
    """Parse one UTF-8 JSON envelope, validate v2, then enforce auth identity."""
    try:
        payload = _load_chunk_payload(raw_body)
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


def parse_gameplay_chunk(
    raw_body: bytes,
    user_id: str,
) -> tuple[ChunkEnvelope, dict[str, object]]:
    """Parse consumer bytes with safe permanent-rejection classifications."""
    try:
        payload = _load_chunk_payload(raw_body)
    except (
        UnicodeDecodeError,
        json.JSONDecodeError,
        ValueError,
        RecursionError,
    ) as error:
        raise PermanentGameplayError(
            "invalid_chunk_json",
            "raw object is not valid UTF-8 JSON",
        ) from error
    if not isinstance(payload, Mapping):
        raise PermanentGameplayError(
            "invalid_chunk_contract",
            "chunk does not satisfy the v2 event contract",
        )
    try:
        envelope = ChunkEnvelope.model_validate(payload)
    except ValidationError as error:
        raise PermanentGameplayError(
            "invalid_chunk_contract",
            "chunk does not satisfy the v2 event contract",
        ) from error
    try:
        envelope.validate_authenticated_identity(user_id)
    except ContractError as error:
        raise PermanentGameplayError(
            "authenticated_identity_mismatch",
            "chunk identity does not match the authenticated pointer",
        ) from error
    return envelope, dict(payload)


def safe_chunk_created_at_ms(raw_body: bytes) -> int | None:
    """Extract only a strict nonnegative chunk timestamp for rejection partitioning."""
    try:
        payload = _load_chunk_payload(raw_body)
    except (
        UnicodeDecodeError,
        json.JSONDecodeError,
        ValueError,
        RecursionError,
    ):
        return None
    if not isinstance(payload, Mapping):
        return None
    created_at_ms = payload.get("created_at_ms")
    if (
        isinstance(created_at_ms, bool)
        or not isinstance(created_at_ms, int)
        or created_at_ms < 0
    ):
        return None
    return created_at_ms
