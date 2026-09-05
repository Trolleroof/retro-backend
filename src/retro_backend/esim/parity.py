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

**What a matching hash does and does not prove.** Parity is judged on
``RestaurantState`` digests, so it covers only what that state carries. The
simulator also holds bookkeeping outside it -- the message inbox most
obviously, which ``restaurant-arena/v1`` has no field for. Two engines can
therefore agree on every tick hash while disagreeing about undelivered
messages. A verdict of ``parity`` means the recorded actions reproduce the
recorded states, not that the engines are identical.
"""

from collections.abc import Iterator, Mapping, Sequence
from typing import Literal, override

from pydantic import Field, ValidationError

from retro_backend.esim.case import ENV_VERSION, CaseError, load_case
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
    # Not in the published v1 schema yet (Trolleroof/tui-gamepigeon#278).
    # Optional so a v1 export still loads: without it the incidents that fire
    # during the episode cannot be recovered and replay does not run.
    case_id: str | None = None
    initial_state: RestaurantState
    events: tuple[TraceEvent, ...] = ()
    consent: Mapping[str, object] | None = None
    deletion: Mapping[str, object] | None = None


class ParityReport(ArenaModel):
    """The finding for one export. Nothing here is inferred."""

    # "parity"   -- replayed and every compared tick matched.
    # "mismatch" -- this engine cannot grade the export as recorded.
    # "unproven" -- comparable, but parity itself was never demonstrated.
    verdict: Literal["parity", "mismatch", "unproven"]
    episode_id: str
    reason: str
    differences: tuple[str, ...] = ()
    ticks_compared: int = 0
    first_divergent_tick: int | None = None


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


# The state a trace event carries is the state at the *start* of its tick,
# before any action taken on it -- the same meaning ``TickSnapshot`` has in
# our own trajectories. Where several events share a tick, the first one is
# the one holding that state. This is the single assumption replay makes
# about trace semantics; it lives here so there is one place to change it if
# Trolleroof/tui-gamepigeon#278 pins it differently.
def _hash_by_tick(trace: ArenaTrace) -> dict[int, str]:
    hashes: dict[int, str] = {}
    for event in trace.events:
        hashes.setdefault(event.tick, event.state_hash)
    return hashes


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


def _replay(trace: ArenaTrace) -> ParityReport:
    """Re-simulate the export against the frozen case it names.

    Only reachable when the trace carries ``caseId``: the incidents come from
    the case's event schedule, which no trace carries today.
    """
    assert trace.case_id is not None
    try:
        case = load_case(trace.case_id)
    except CaseError as exc:
        return ParityReport(
            verdict="mismatch",
            episode_id=trace.episode_id,
            reason=f"export names case {trace.case_id!r}, which this engine cannot load: {exc}",
        )

    expected = _hash_by_tick(trace)
    scheduled = _envelopes_by_tick(trace)
    sim = RestaurantSim(case.initial_state, event_schedule=case.event_schedule)
    state = sim.reset(trace.seed)

    compared = 0
    while True:
        tick = state.episode.tick
        recorded = expected.get(tick)
        if recorded is not None:
            compared += 1
            ours = state_hash(state.model_dump(by_alias=True, mode="json"))
            if ours != recorded:
                return ParityReport(
                    verdict="mismatch",
                    episode_id=trace.episode_id,
                    reason=(
                        f"replay diverged at tick {tick}: the export records "
                        f"{recorded} and this engine reached {ours}. The two engines "
                        f"do not agree on what the recorded actions produce."
                    ),
                    ticks_compared=compared,
                    first_divergent_tick=tick,
                )
        if state.episode.phase != "active":
            break
        for envelope in scheduled.get(tick, ()):
            result = sim.act(envelope)
            # A rejection is recorded state too: the export may have recorded
            # an action the simulator refused, and forcing it through would
            # replay an episode that never happened.
            if not isinstance(result, ActionError):
                state = result
        state = sim.advance_time()

    if compared == 0:
        return ParityReport(
            verdict="unproven",
            episode_id=trace.episode_id,
            reason=(
                "replay ran but the export carries no state hash at any tick this "
                "engine reached, so nothing was actually compared."
            ),
        )
    return ParityReport(
        verdict="parity",
        episode_id=trace.episode_id,
        reason=f"replayed against case {trace.case_id!r}; {compared} tick(s) matched.",
        ticks_compared=compared,
    )


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

    if trace.case_id is None:
        return ParityReport(
            verdict="unproven",
            episode_id=trace.episode_id,
            reason=(
                "hashes are comparable, but the export names no caseId and a trace carries no "
                "event schedule of its own. The incidents that fire during an episode are "
                "constructor state, not trace events, so replaying the actions without them "
                "would diverge for a missing-input reason and misreport it as an engine "
                "disagreement. Semantic parity is untested (see #78)."
            ),
        )
    return _replay(trace)
