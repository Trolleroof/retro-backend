"""Cross-engine parity: can a foreign trace be graded by this engine? (#72)

The Tui client runs its own copy of the restaurant and exports a
``restaurant-arena-trace/v1`` or ``/v3``. This checks whether such an export
is one this engine could grade, and says plainly when it is not.

**A dead end worth recording.** The original design hashed the export's raw
JSON and compared it with that same JSON parsed into our models and dumped
back, on the theory that a float written ``70`` instead of ``70.0`` (all
JavaScript integers serialize without a decimal point) would show up as a
false mismatch. That framing was wrong: comparing pre-parse bytes to
post-parse bytes of *the same input* always disagrees on every integral
float and every optional field, whether or not the underlying restaurant
state matches at all -- a self-comparison, not a check against anything.
Also, ``initialState`` is plain JSON produced by a JS runtime; it cannot be
made to spell an integer ``70.0`` short of a hand-written string serializer,
which was never actually shipped for it. :func:`check_parity` instead
compares the export against genuine ground truth -- this engine's own
``sim.reset(seed)`` for the case the trace names -- so wire-format number
spelling plays no part in the result at all.

**What this will not do.** It does not rewrite events, coerce an unknown role
onto a known one, or round numbers to make digests agree. When the check cannot
be completed the verdict says so; no verdict means "probably fine".

**What a matching hash does and does not prove.** Parity is judged on
``RestaurantState`` digests, so it covers only what that state carries. The
simulator also holds bookkeeping outside it -- the message inbox most
obviously, which ``restaurant-arena/v1`` has no field for. Two engines can
therefore agree on every tick hash while disagreeing about undelivered
messages. A verdict of ``parity`` means the recorded actions reproduce the
recorded states, not that the engines are identical.

**Three questions the Tui side answered directly**
(Trolleroof/tui-gamepigeon#277, comment 5554247493):

1. ``envVersion`` is the Tui *application build*, kept in step with its own
   package version -- it is not a simulator revision and is never compared
   with this engine's ``ENV_VERSION``. Only ``contractVersion`` gates parity.
2. ``events[].stateHash`` and ``replay.inputs[].stateHash`` are **not**
   ``RestaurantState`` hashes. The former is an event-payload fingerprint
   (for the initial event, exactly ``{seed, episodeId, tick}``); the latter
   hashes the native Tui runtime checkpoint -- RNG state, queues, inboxes,
   and more, not the wire state alone. Only ``initialState`` is a genuine
   ``RestaurantState``, so it is the only field this module hashes for
   comparison. Per-tick replay parity needs Tui to expose a state-only
   checkpoint hash, which does not exist yet -- see :func:`_replay`.
3. A nullable field is exported *absent*, never as JSON ``null``. Backend
   normalization strips ``None`` optional fields before hashing so both
   sides describe the same shape; see :func:`_drop_none`.
"""

from collections.abc import Iterator, Mapping, Sequence
from typing import Literal, override

from pydantic import Field, ValidationError

from retro_backend.esim.case import CaseError, load_case
from retro_backend.esim.contracts import (
    STATE_VERSION,
    ActionEnvelope,
    ActionError,
    ArenaId,
    ArenaModel,
    RestaurantState,
    Role,
)
from retro_backend.esim.simulator import RestaurantSim
from retro_backend.esim.state_hash import STATE_HASH_PATTERN, state_hash

TRACE_VERSION_V1 = "restaurant-arena-trace/v1"
TRACE_VERSION_V3 = "restaurant-arena-trace/v3"
# Enough differing fields to see the pattern, not so many that the real one
# scrolls away: a whole-state spelling mismatch names every numeric field.
MAX_REPORTED_DIFFERENCES = 10

__all__ = [
    "TRACE_VERSION_V1",
    "TRACE_VERSION_V3",
    "ArenaTrace",
    "ParityReport",
    "TraceEvent",
    "TraceLoadError",
    "check_parity",
    "load_trace",
]

TraceEventKind = Literal[
    "observation",
    "action",
    "player_correction",
    "message",
    "outcome",
    # v3 additions (Trolleroof/tui-gamepigeon#276): the inspection-workflow
    # events. Modelled even though this check does not read them, per the
    # class docstring below -- a valid v3 export must not be refused for a
    # kind we chose not to keep up with.
    "inspection",
    "hold",
    "hold_cleared",
    "verification_request",
    "verification_response",
    "escalation",
    "handoff",
    "supply_order",
    "supply_delivered",
    "stockout",
]


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
    # v3: what an inspection-kind event is about, and its body.
    subject: Mapping[str, object] | None = None
    payload: Mapping[str, object] | None = None


class ArenaTrace(ArenaModel):
    """A foreign engine's export of one episode."""

    version: Literal["restaurant-arena-trace/v1", "restaurant-arena-trace/v3"] = TRACE_VERSION_V1
    episode_id: ArenaId
    seed: int = Field(ge=0)
    env_version: str = Field(min_length=1)
    contract_version: str
    run_kind: Literal["live", "synthetic", "benchmark"]
    # Not in the published v1 schema yet (Trolleroof/tui-gamepigeon#278).
    # Optional so a v1 export still loads: without it the incidents that fire
    # during the episode cannot be recovered and replay does not run.
    case_id: str | None = None
    initial_state: RestaurantState
    events: tuple[TraceEvent, ...] = ()
    consent: Mapping[str, object] | None = None
    deletion: Mapping[str, object] | None = None
    # v3: episode-end status and the exporter's own replay log. Not read yet
    # -- what `stateHash` is computed over is still an open question with the
    # Tui side (Trolleroof/tui-gamepigeon#277) -- but a valid v3 export must
    # load, and refusing an unrecognized-but-present field would be exactly
    # that.
    terminal: Mapping[str, object] | None = None
    replay: Mapping[str, object] | None = None


class ParityReport(ArenaModel):
    """The finding for one export. Nothing here is inferred."""

    # "parity"   -- replayed and every compared tick matched.
    # "mismatch" -- this engine cannot grade the export as recorded.
    # "unproven" -- comparable, but parity itself was never demonstrated.
    verdict: Literal["parity", "mismatch", "unproven"]
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
            f"not a {TRACE_VERSION_V1} or {TRACE_VERSION_V3} ({exc.error_count()} field error(s))\n{exc}"
        ) from exc


def _drop_none(value: object) -> object:
    """Strip ``None`` from optional fields before hashing or diffing.

    Confirmed by the Tui side: the wire contract makes these fields optional
    and never permits explicit JSON ``null``, while backend models default an
    absent field to ``None``. Stripping it here puts both sides back into the
    one shape the contract actually allows, rather than reporting a
    difference that is only an artifact of how Pydantic represents "absent".
    """
    if isinstance(value, Mapping):
        return {k: _drop_none(v) for k, v in value.items() if v is not None}
    if isinstance(value, Sequence) and not isinstance(value, str):
        return [_drop_none(v) for v in value]
    return value


class _Absent:
    """Marks a key or index one side does not have.

    ``None`` cannot stand in for this. An omitted nullable field and one
    explicitly set to ``null`` hash differently -- canonical JSON carries the
    key in one case and not the other -- but both read as ``None`` through
    ``Mapping.get``. Conflating them let a mismatch be reported with "0
    field(s) differ", naming nothing and pointing at nothing.
    """

    @override
    def __repr__(self) -> str:
        return "absent"


ABSENT = _Absent()


def _differences(exported: object, ours: object, path: str = "") -> Iterator[str]:
    """Yield ``path: exported=X engine=Y`` for every leaf that differs."""
    if isinstance(exported, Mapping) and isinstance(ours, Mapping):
        for key in sorted(set(exported) | set(ours)):
            yield from _differences(
                exported.get(key, ABSENT), ours.get(key, ABSENT), f"{path}/{key}"
            )
        return
    if (
        isinstance(exported, Sequence)
        and isinstance(ours, Sequence)
        and not isinstance(exported, str)
        and not isinstance(ours, str)
    ):
        for index in range(max(len(exported), len(ours))):
            left = exported[index] if index < len(exported) else ABSENT
            right = ours[index] if index < len(ours) else ABSENT
            yield from _differences(left, right, f"{path}[{index}]")
        return
    # repr, not ==, because 70 and 70.0 compare equal and are the whole point.
    if repr(exported) != repr(ours):
        yield f"{path or '/'}: exported={exported!r} engine={ours!r}"


def _envelopes_by_tick(trace: ArenaTrace) -> dict[int, list[ActionEnvelope]]:
    """Group the export's actions by tick, in the order it recorded them.

    An ``action`` payload that is not a valid ``restaurant-arena-action/v1``
    is raised rather than skipped. Dropping it would replay a shorter episode
    than the one exported and then report the resulting divergence as an
    engine disagreement.
    """
    actions: dict[int, list[ActionEnvelope]] = {}
    for index, event in enumerate(trace.events):
        if event.kind != "action" or event.action is None:
            continue
        try:
            envelope = ActionEnvelope.model_validate(dict(event.action))
        except ValidationError as exc:
            where = f"event {index} (tick {event.tick})"
            detail = f"{exc.error_count()} field error(s)"
            raise TraceLoadError(
                f"{where} carries an action this engine cannot read: {detail}\n{exc}"
            ) from exc
        actions.setdefault(event.tick, []).append(envelope)
    return actions


def _replay(trace: ArenaTrace, sim: RestaurantSim, state: RestaurantState) -> ParityReport:
    """Apply every recorded action against ``sim``, which already sits at
    the reset state this trace's initial state was checked against.

    This executes the recorded actions and confirms every one is readable
    and applies without raising -- but it cannot verify the resulting state,
    because the trace carries no hash comparable with ``RestaurantState``.
    Confirmed by the Tui side (Trolleroof/tui-gamepigeon#277): both
    ``events[].stateHash`` and ``replay.inputs[].stateHash`` are different
    hash families (an event fingerprint, and a native-runtime checkpoint
    hash respectively), not a ``RestaurantState`` digest. Comparing against
    either would validate against a hash that means something else and could
    report ``parity`` on a run this engine never actually verified -- worse
    than not checking. So this never returns ``parity``; it stays
    ``unproven`` until Tui exposes a state-only checkpoint hash per tick.
    """
    scheduled = _envelopes_by_tick(trace)
    replayed = 0
    while state.episode.phase == "active":
        for envelope in scheduled.get(state.episode.tick, ()):
            result = sim.act(envelope)
            # A rejection is recorded state too: the export may have recorded
            # an action the simulator refused, and forcing it through would
            # replay an episode that never happened.
            if not isinstance(result, ActionError):
                state = result
            replayed += 1
        state = sim.advance_time()

    return ParityReport(
        verdict="unproven",
        episode_id=trace.episode_id,
        reason=(
            f"initial state matched this engine's own reset for case {trace.case_id!r} at "
            f"seed {trace.seed}; replayed {replayed} action(s) against it without error. But "
            f"the export carries no state-only checkpoint hash to verify the result against -- "
            f"its recorded hashes are a different family (see #277). Semantic parity remains "
            f"untested."
        ),
    )


def check_parity(payload: Mapping[str, object]) -> ParityReport:
    """Report whether this engine can grade ``payload`` as recorded.

    The export's ``initialState`` is checked against this engine's own
    ``sim.reset(seed)`` for the case it names -- genuine ground truth, not
    a comparison against itself. An earlier version of this function hashed
    the export's raw JSON against that same JSON re-serialized through our
    model, which always differed on every optional field and every integral
    float: Pydantic coerces an int into a float on a float-typed field and
    fills an absent key with ``None`` regardless of what the export actually
    represents. That was never a report of the two engines disagreeing about
    the restaurant -- it was an artifact of comparing pre-parse bytes to
    post-parse bytes, confirmed by the Tui side (#277) once asked what
    ``initialState`` was actually supposed to be checked against.
    """
    trace = load_trace(payload)
    exported_state = payload.get("initialState")
    if not isinstance(exported_state, Mapping):
        raise TraceLoadError("initialState is missing or not an object")

    if trace.contract_version != STATE_VERSION:
        return ParityReport(
            verdict="mismatch",
            episode_id=trace.episode_id,
            reason=(
                f"export declares contract {trace.contract_version}; this engine is "
                f"{STATE_VERSION}. States are not known to mean the same thing, so nothing "
                f"was compared. (envVersion is the producer's own build version, confirmed "
                f"not comparable with this engine's revision, and is not gated on.)"
            ),
        )

    if trace.case_id is None:
        return ParityReport(
            verdict="unproven",
            episode_id=trace.episode_id,
            reason=(
                "the export names no caseId, so this engine has no ground truth to check "
                "initialState against and no event schedule to replay against -- the "
                "incidents that fire during an episode are constructor state on the frozen "
                "case, not trace events. Nothing about this export can be verified (see #278)."
            ),
        )

    try:
        case = load_case(trace.case_id)
    except CaseError as exc:
        return ParityReport(
            verdict="mismatch",
            episode_id=trace.episode_id,
            reason=f"export names case {trace.case_id!r}, which this engine cannot load: {exc}",
        )

    sim = RestaurantSim(case.initial_state, event_schedule=case.event_schedule)
    reset_state = sim.reset(trace.seed)
    expected = _drop_none(reset_state.model_dump(by_alias=True, mode="json"))
    exported_norm = _drop_none(trace.initial_state.model_dump(by_alias=True, mode="json"))

    if state_hash(expected) != state_hash(exported_norm):
        differences = list(_differences(exported_norm, expected))
        shown = differences[:MAX_REPORTED_DIFFERENCES]
        if len(differences) > len(shown):
            shown.append(f"... and {len(differences) - len(shown)} more field(s)")
        return ParityReport(
            verdict="mismatch",
            episode_id=trace.episode_id,
            reason=(
                f"export's initialState does not match what this engine produces for case "
                f"{trace.case_id!r} at seed {trace.seed} ({len(differences)} field(s) differ)."
            ),
            differences=tuple(shown),
        )

    return _replay(trace, sim, reset_state)
