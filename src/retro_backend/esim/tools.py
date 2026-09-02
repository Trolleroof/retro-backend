"""Per-tool argument schemas and the role permission matrix (#57).

``docs/restaurant-arena-tools.md`` names eight tools and says the simulator
validates role permission, required fields, entity existence, current state,
and safety constraints -- but leaves the per-tool argument shapes and the
role-to-tool matrix as prose. This module is the enumeration those checks run
against.

Known gap: ``send_message``'s recipient-visibility promise ("visible to the
recipient on its next observation") cannot be fully honored by the frozen
``restaurant-arena-observation/v1`` schema today -- no observation carries an
inbox field, and only the expediter's observation lists other workers at all
(without ``lastAction``). This module records the message on the recipient's
``WorkerState.last_action`` in the full state (visible in checkpoints and to
the expediter), which is the closest available representation; a dedicated
inbox field is a contract change for a future version, not something to
smuggle into ``restaurant-arena/v1`` here.
"""

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field
from pydantic.alias_generators import to_camel

from retro_backend.esim.contracts import ArenaId, Role, Tool

# Communication and escalation are available to every role; the rest are
# scoped to the one role whose job description in
# docs/restaurant-arena-demo.md names it.
ROLE_TOOLS: dict[Role, frozenset[Tool]] = {
    "chef": frozenset({"inspect", "prepare", "request_help", "send_message"}),
    "server": frozenset({"serve", "update_guest", "request_help", "send_message"}),
    "host": frozenset({"seat", "update_guest", "request_help", "send_message"}),
    "expediter": frozenset(
        {"inspect", "coordinate", "request_help", "send_message"}
    ),
}


class ToolArgs(BaseModel):
    """Frozen, closed, camelCase-on-the-wire argument model.

    Matches ``ArenaModel`` (contracts.py): extra or missing fields are
    rejected, and the wire format is camelCase.
    """

    model_config = ConfigDict(
        extra="forbid",
        frozen=True,
        populate_by_name=True,
        alias_generator=to_camel,
    )


class InspectArgs(ToolArgs):
    """Read-only look at an order or equipment item. Never mutates state."""

    target_id: ArenaId


class PrepareArgs(ToolArgs):
    order_id: ArenaId


class ServeArgs(ToolArgs):
    order_id: ArenaId
    # Required true for an allergy order before it may be served -- the
    # demo's core safety rule (docs/restaurant-arena-demo.md step 4/6).
    # Defaults false so a caller cannot serve an allergy order by omission.
    verified: bool = False


class SeatArgs(ToolArgs):
    table_id: ArenaId
    party_size: int = Field(ge=1, strict=True)


class UpdateGuestArgs(ToolArgs):
    note: str = Field(min_length=1, max_length=256)
    order_id: ArenaId | None = None
    table_id: ArenaId | None = None


class CoordinateArgs(ToolArgs):
    worker_id: ArenaId
    order_id: ArenaId | None = None
    priority: Literal["normal", "high"] | None = None


class RequestHelpArgs(ToolArgs):
    worker_id: ArenaId
    reason: str = Field(min_length=1, max_length=256)


class SendMessageArgs(ToolArgs):
    to_worker_id: ArenaId
    text: str = Field(min_length=1, max_length=256)
    priority: Literal["normal", "high"] = "normal"


TOOL_ARGS: dict[Tool, type[ToolArgs]] = {
    "inspect": InspectArgs,
    "prepare": PrepareArgs,
    "serve": ServeArgs,
    "seat": SeatArgs,
    "update_guest": UpdateGuestArgs,
    "coordinate": CoordinateArgs,
    "request_help": RequestHelpArgs,
    "send_message": SendMessageArgs,
}


def role_permits(role: Role, tool: Tool) -> bool:
    return tool in ROLE_TOOLS[role]
