"""Cross-engine parity: can a foreign trace be graded by this engine? (#72)

The Tui client runs its own copy of the restaurant and exports a
``restaurant-arena-trace/v1``. This checks whether such an export is one this
engine could grade, and says plainly when it is not.

**The known incompatibility.** ``state_hash`` writes a float as ``1.0``;
JavaScript has one number type and ``JSON.stringify`` writes ``1``. Two engines
can hold the same restaurant and publish different digests for it. The agreed
fix is that the exporter writes ``70.0`` -- this engine's spelling is the
contract, and nothing here normalizes numbers to paper over a mismatch.

**What this will not do.** It does not rewrite events, coerce an unknown role
onto a known one, or round numbers to make digests agree. When the check cannot
be completed the verdict says so; no verdict means "probably fine".
"""

from collections.abc import Iterator, Mapping, Sequence
from typing import Literal

from pydantic import Field, ValidationError

from retro_backend.esim.case import ENV_VERSION
from retro_backend.esim.contracts import (
    STATE_VERSION,
    ArenaId,
    ArenaModel,
    RestaurantState,
    Role,
)
from retro_backend.esim.state_hash import STATE_HASH_PATTERN, state_hash

TRACE_VERSION = "restaurant-arena-trace/v1"
# Enough differing fields to see the pattern, not so many that the real one
# scrolls away: a whole-state spelling mismatch names every numeric field.
MAX_REPORTED_DIFFERENCES = 10

__all__ = [
    "TRACE_VERSION",
    "ArenaTrace",
    "ParityReport",
    "TraceEvent",
    "TraceLoadError",
    "check_parity",
    "load_trace",
]

TraceEventKind = Literal["observation", "action", "player_correction", "message", "outcome"]


class TraceLoadError(Exception):
    """An export could not be read as a ``restaurant-arena-trace/v1``."""


class TraceEvent(ArenaModel):
    """One event in a foreign trace.

    Every optional field the v1 schema allows is modelled, even the ones this
    check never reads. ``ArenaModel`` forbids extras, so omitting them would
    turn a valid export into an unreadable one -- a diagnostic we invented.
    """

    tick: int = Field(ge=0)
    kind: TraceEventKind
    timestamp: str
    state_hash: str = Field(pattern=STATE_HASH_PATTERN)
    actor_id: ArenaId | None = None
    # Typed against our own Role on purpose. An export naming a role this
    # engine does not have is a compatibility failure to report, never a
    # role to map onto the nearest match.
    role: Role | None = None
    target_role: Role | None = None
    checkpoint_id: ArenaId | None = None
    model: Mapping[str, object] | None = None
    request_id: ArenaId | None = None
    idempotency_key: str | None = None
    text: str | None = None
    observation: Mapping[str, object] | None = None
    action: Mapping[str, object] | None = None
    tool_result: Mapping[str, object] | None = None
    outcome: Mapping[str, object] | None = None
    correction_rationale: str | None = None
    correction_category: str | None = None
    changed_decision_event_id: ArenaId | None = None
    probe_event_id: ArenaId | None = None


class ArenaTrace(ArenaModel):
    """A foreign engine's export of one episode."""

    version: Literal["restaurant-arena-trace/v1"] = TRACE_VERSION
    episode_id: ArenaId
    seed: int = Field(ge=0)
    env_version: str = Field(min_length=1)
    contract_version: str
    run_kind: Literal["live", "synthetic", "benchmark"]
    initial_state: RestaurantState
    events: tuple[TraceEvent, ...] = ()
    consent: Mapping[str, object] | None = None
    deletion: Mapping[str, object] | None = None


class ParityReport(ArenaModel):
    """The finding for one export. Nothing here is inferred."""

    # "mismatch" means this engine cannot grade the export as recorded.
    # "unproven" means it could, but parity itself was never demonstrated.
    # There is deliberately no verdict meaning "parity proven".
    verdict: Literal["mismatch", "unproven"]
    episode_id: str
    reason: str
    differences: tuple[str, ...] = ()


def load_trace(payload: object) -> ArenaTrace:
    """Parse an export, naming what made it unreadable.

    A refusal here is a real result: an export carrying a role this engine
    does not define must fail loudly, not be reshaped into one that parses.
    """
    try:
        return ArenaTrace.model_validate(payload)
    except ValidationError as exc:
        raise TraceLoadError(
            f"not a {TRACE_VERSION} ({exc.error_count()} field error(s))\n{exc}"
        ) from exc


def _differences(exported: object, ours: object, path: str = "") -> Iterator[str]:
    """Yield ``path: exported=X engine=Y`` for every leaf that differs."""
    if isinstance(exported, Mapping) and isinstance(ours, Mapping):
        for key in sorted(set(exported) | set(ours)):
            yield from _differences(exported.get(key), ours.get(key), f"{path}/{key}")
        return
    if (
        isinstance(exported, Sequence)
        and isinstance(ours, Sequence)
        and not isinstance(exported, str)
        and not isinstance(ours, str)
    ):
        for index in range(max(len(exported), len(ours))):
            left = exported[index] if index < len(exported) else None
            right = ours[index] if index < len(ours) else None
            yield from _differences(left, right, f"{path}[{index}]")
        return
    # repr, not ==, because 70 and 70.0 compare equal and are the whole point.
    if repr(exported) != repr(ours):
        yield f"{path or '/'}: exported={exported!r} engine={ours!r}"


def check_parity(payload: Mapping[str, object]) -> ParityReport:
    """Report whether this engine can grade ``payload`` as recorded.

    Hashes are compared over the *same* restaurant -- the export's raw JSON,
    and that JSON parsed into our models and dumped back -- so a mismatch is
    introduced by serialization alone and is the exporter's to fix.
    """
    trace = load_trace(payload)
    exported_state = payload.get("initialState")
    if not isinstance(exported_state, Mapping):
        raise TraceLoadError("initialState is missing or not an object")

    if trace.contract_version != STATE_VERSION or trace.env_version != ENV_VERSION:
        return ParityReport(
            verdict="mismatch",
            episode_id=trace.episode_id,
            reason=(
                f"export declares contract {trace.contract_version} / env {trace.env_version}; "
                f"this engine is {STATE_VERSION} / {ENV_VERSION}. States are not known to mean "
                f"the same thing, so nothing was compared."
            ),
        )

    ours = trace.initial_state.model_dump(by_alias=True, mode="json")
    if state_hash(dict(exported_state)) != state_hash(ours):
        differences = list(_differences(exported_state, ours))
        shown = differences[:MAX_REPORTED_DIFFERENCES]
        if len(differences) > len(shown):
            shown.append(f"... and {len(differences) - len(shown)} more field(s)")
        return ParityReport(
            verdict="mismatch",
            episode_id=trace.episode_id,
            reason=(
                f"the two engines hash the same restaurant differently "
                f"({len(differences)} field(s) differ). Per-tick comparison would fail for this "
                f"reason alone, so none was run. Integral floats must be exported as 70.0, not 70."
            ),
            differences=tuple(shown),
        )

    return ParityReport(
        verdict="unproven",
        episode_id=trace.episode_id,
        reason=(
            "hashes are comparable, but a trace carries no event schedule: the incidents that "
            "fire during an episode are constructor state, not trace events. Replaying the "
            "actions without them would diverge for a missing-input reason and misreport it as "
            "an engine disagreement. Semantic parity is untested."
        ),
    )
