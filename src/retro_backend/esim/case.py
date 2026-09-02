"""Frozen case loading and repeatable ``reset(case_id, seed)`` (#58).

A benchmark run has to compare models against *exactly* the same starting
point every time. ``FrozenCase`` is that starting point, pinned to disk under
``contracts/esim/cases/<case_id>.json`` and loaded through
:func:`load_case`, which validates against the published
``restaurant-arena-state-v1`` schema (#54) before anything touches
:class:`~retro_backend.esim.simulator.RestaurantSim`.

Freezing follows tui_oracle.md §3: environment/rules version, seed, initial
state, event schedule, role permissions and available tools, and time
horizon. Turn budget is not in this issue's field list and is not
implemented here.
"""

import json
from collections.abc import Mapping, Sequence
from pathlib import Path

from jsonschema import Draft202012Validator
from jsonschema.exceptions import ValidationError as JsonSchemaValidationError
from pydantic import BaseModel, ConfigDict, Field
from pydantic import ValidationError as PydanticValidationError
from pydantic.alias_generators import to_camel

from retro_backend.esim.contracts import ACTION_VERSION, STATE_VERSION, RestaurantState, Role, Tool
from retro_backend.esim.simulator import RestaurantSim, ScheduledIncident
from retro_backend.esim.tools import ROLE_TOOLS

# The frozen environment/rules version this module implements. A case
# authored against a different value is rejected outright (#58 acceptance:
# "a mismatch is rejected rather than coerced"), never silently upgraded.
ENV_VERSION = "restaurant-e-sim/v1"

_CASES_DIR = Path(__file__).resolve().parents[3] / "contracts/esim/cases"
_STATE_SCHEMA_PATH = (
    Path(__file__).resolve().parents[3] / "contracts/restaurant-arena-state-v1.schema.json"
)
_STATE_SCHEMA = json.loads(_STATE_SCHEMA_PATH.read_text())


class CaseError(Exception):
    """Base class for every ``load_case``/``reset`` failure."""


class CaseNotFoundError(CaseError):
    """No case file exists for the requested ``case_id``."""


class CaseSchemaError(CaseError):
    """The case file exists but does not parse or does not validate."""


class CaseVersionError(CaseError):
    """The case declares an ``envVersion`` or ``contractVersion`` this module does not implement."""


class FrozenCase(BaseModel):
    """One resettable, versioned scenario.

    Everything a benchmark run needs to reproduce identical conditions:
    which environment version graded it, which wire contract version its
    state speaks, the starting state itself, the incidents that will fire
    and when, which tools each role may use, and the episode horizon.
    """

    model_config = ConfigDict(
        extra="forbid",
        frozen=True,
        populate_by_name=True,
        alias_generator=to_camel,
    )

    case_id: str = Field(min_length=1, max_length=64)
    env_version: str
    contract_version: str
    seed: int = Field(ge=0, strict=True)
    initial_state: RestaurantState
    event_schedule: tuple[ScheduledIncident, ...]
    tool_schema_version: str
    role_permissions: Mapping[Role, tuple[Tool, ...]]
    horizon: int = Field(ge=1, strict=True)


def _case_path(case_id: str) -> Path:
    # case_id is used only to build a filename under a fixed directory, and
    # FrozenCase's own pattern (ArenaId-shaped in the source JSON, checked
    # by Pydantic on parse) already excludes path separators and "..". This
    # check runs before that parse, so it re-asserts the same constraint
    # rather than trusting the caller's raw string.
    if "/" in case_id or "\\" in case_id or case_id in ("", ".", ".."):
        raise CaseNotFoundError(f"invalid case id {case_id!r}")
    return _CASES_DIR / f"{case_id}.json"


def load_case(case_id: str) -> FrozenCase:
    """Load, version-check, and schema-validate one frozen case.

    Raises :class:`CaseNotFoundError`, :class:`CaseSchemaError`, or
    :class:`CaseVersionError`; never returns a partially-valid case.
    """
    path = _case_path(case_id)
    if not path.is_file():
        raise CaseNotFoundError(f"no frozen case at {path}")

    try:
        payload = json.loads(path.read_text())
    except json.JSONDecodeError as exc:
        raise CaseSchemaError(f"{path} is not valid JSON: {exc}") from exc

    if not isinstance(payload, dict):
        raise CaseSchemaError(f"{path} must contain a JSON object")

    env_version = payload.get("envVersion")
    if env_version != ENV_VERSION:
        message = f"case {case_id!r} declares envVersion {env_version!r}, this module implements {ENV_VERSION!r}"
        raise CaseVersionError(message)
    contract_version = payload.get("contractVersion")
    if contract_version != STATE_VERSION:
        message = f"case {case_id!r} declares contractVersion {contract_version!r}, this module implements {STATE_VERSION!r}"
        raise CaseVersionError(message)
    tool_schema_version = payload.get("toolSchemaVersion")
    if tool_schema_version != ACTION_VERSION:
        message = f"case {case_id!r} declares toolSchemaVersion {tool_schema_version!r}, this module implements {ACTION_VERSION!r}"
        raise CaseVersionError(message)

    role_permissions = payload.get("rolePermissions")
    expected_permissions: dict[str, Sequence[str]] = {
        role: list(tools) for role, tools in default_role_permissions().items()
    }
    if not _same_permissions(role_permissions, expected_permissions):
        # Case-level permission overrides are declared but not yet wired
        # into RestaurantSim, which enforces the global #57 matrix. Rather
        # than accept a declaration the simulator would silently ignore,
        # reject any case that diverges from the matrix actually enforced.
        message = f"case {case_id!r} rolePermissions do not match the enforced #57 matrix (per-case permission overrides are not implemented yet)"
        raise CaseVersionError(message)

    initial_state = payload.get("initialState")
    if not isinstance(initial_state, dict):
        raise CaseSchemaError(f"case {case_id!r} is missing an object initialState")
    schema_errors = list(Draft202012Validator(_STATE_SCHEMA).iter_errors(initial_state))
    if schema_errors:
        message = f"case {case_id!r} initialState fails restaurant-arena-state-v1: {_first_error_message(schema_errors[0])}"
        raise CaseSchemaError(message)

    try:
        return FrozenCase.model_validate(payload)
    except PydanticValidationError as exc:
        raise CaseSchemaError(f"case {case_id!r} failed contract validation: {exc}") from exc


def _same_permissions(
    declared: object, expected: Mapping[str, Sequence[str]]
) -> bool:
    if not isinstance(declared, dict):
        return False
    if set(declared) != set(expected):
        return False
    for role, tools in expected.items():
        value = declared.get(role)
        if not isinstance(value, list) or set(value) != set(tools):
            return False
    return True


def _first_error_message(error: JsonSchemaValidationError) -> str:
    path = "/".join(str(part) for part in error.absolute_path)
    return f"{path or '<root>'}: {error.message}"


def reset(case_id: str, seed: int) -> RestaurantSim:
    """Load ``case_id`` and return a ``RestaurantSim`` reset to ``seed``.

    Repeatable: the same ``case_id`` and ``seed`` produce the identical
    initial state (and therefore identical ``state_hash``) on every fresh
    call, since ``load_case`` re-reads and re-validates the case file each
    time rather than caching mutable state between calls.
    """
    case = load_case(case_id)
    sim = RestaurantSim(case.initial_state, event_schedule=case.event_schedule)
    sim.reset(seed)
    return sim


def default_role_permissions() -> Mapping[Role, tuple[Tool, ...]]:
    """The current (#57) role-to-tool matrix, for authoring new cases."""
    return {role: tuple(sorted(tools)) for role, tools in ROLE_TOOLS.items()}


__all__ = [
    "ACTION_VERSION",
    "ENV_VERSION",
    "CaseError",
    "CaseNotFoundError",
    "CaseSchemaError",
    "CaseVersionError",
    "FrozenCase",
    "default_role_permissions",
    "load_case",
    "reset",
]
