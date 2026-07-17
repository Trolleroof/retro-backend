"""Pure-Python request contracts shared by Worker HTTP and Queue handlers."""

import re
from typing import Literal

from pydantic import AwareDatetime, BaseModel, ConfigDict, Field, model_validator

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
