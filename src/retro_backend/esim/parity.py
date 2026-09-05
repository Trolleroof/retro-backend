"""Cross-engine parity: can a foreign trace be graded by this engine? (#72)

The Tui client runs its own copy of the restaurant and exports a
``restaurant-arena-trace/v1``. This module answers one question about such an
export -- whether its state hashes are comparable with ours at all -- and
refuses to answer questions it cannot.

**Why hashes are the thing to check first.** ``state_hash`` canonicalizes with
Python's JSON spelling, where a float is written ``1.0``. JavaScript has one
number type and ``JSON.stringify`` writes ``1``. Two engines can therefore hold
byte-identical restaurant state and still publish different digests for it.
:func:`check_encoding` proves which case an export is in *before* any tick is
compared, because a mismatched digest otherwise reads as a simulator
disagreement when it is only a serializer disagreement.

**What this module will not do.** It does not rewrite events, coerce an unknown
role onto a known one, or round numbers to make digests agree. Every one of
those would manufacture a parity result. When the check cannot be completed the
verdict says so; there is no verdict that means "probably fine".
"""

import math
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

__all__ = [
    "TRACE_VERSION",
    "ArenaTrace",
    "EncodingCheck",
    "FieldDivergence",
    "ParityReport",
    "TraceEvent",
    "TraceLoadError",
    "check_encoding",
    "check_parity",
    "numerically_equal",
    "load_trace",
]

TraceEventKind = Literal["observation", "action", "player_correction", "message", "outcome"]
Verdict = Literal[
    "contract_mismatch",
    "encoding_divergence",
    "parity_unproven",
]


class TraceLoadError(Exception):
    """An export could not be read as a ``restaurant-arena-trace/v1``."""


class TraceEvent(ArenaModel):
    """One event in a foreign trace.

    Every optional field the v1 schema allows is modelled, even the ones
    parity never reads. ``ArenaModel`` forbids extras, so omitting them would
    turn a valid export into an unreadable one -- a false diagnostic, and the
    kind of thing this module exists to avoid producing.
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


class FieldDivergence(ArenaModel):
    """One field whose spelling differs between the two engines."""

    path: str
    exported: str
    normalized: str
    # True when the two values are numerically equal and only their JSON
    # spelling differs (``1`` against ``1.0``). False means the engines hold
    # genuinely different state, which no serializer fix would reconcile.
    numeric_spelling_only: bool


class EncodingCheck(ArenaModel):
    """Whether this engine hashes the export's own state the way it does.

    Both digests are taken over the *same* restaurant: the export's raw JSON,
    and that JSON parsed into our typed models and dumped back. Any difference
    is introduced by serialization alone, so a mismatch proves the two engines'
    digests are not comparable -- independent of who simulated what.
    """

    exported_hash: str
    normalized_hash: str
    agrees: bool
    divergences: tuple[FieldDivergence, ...] = ()


class ParityReport(ArenaModel):
    """The full finding for one export. Nothing here is inferred."""

    verdict: Verdict
    episode_id: str
    trace_env_version: str
    engine_env_version: str = ENV_VERSION
    trace_contract_version: str
    engine_contract_version: str = STATE_VERSION
    encoding: EncodingCheck | None = None
    replay_attempted: bool = False
    # Present whenever replay did not run. Parity is never claimed on the
    # strength of a check that was skipped.
    blocked_reason: str | None = None

    @property
    def comparable(self) -> bool:
        """True only when this engine could grade the export as recorded."""
        return self.verdict not in ("contract_mismatch", "encoding_divergence")


def load_trace(payload: object) -> ArenaTrace:
    """Parse an export, naming what made it unreadable.

    A refusal here is a real result. An export carrying a role or tool this
    engine does not define must fail loudly rather than be reshaped into one
    that parses.
    """
    try:
        return ArenaTrace.model_validate(payload)
    except ValidationError as exc:
        raise TraceLoadError(
            f"not a {TRACE_VERSION} ({exc.error_count()} field error(s))\n{exc}"
        ) from exc


def _walk(exported: object, normalized: object, path: str = "") -> Iterator[FieldDivergence]:
    """Yield every leaf whose JSON spelling differs between the two trees."""
    if isinstance(exported, Mapping) and isinstance(normalized, Mapping):
        for key in sorted(set(exported) | set(normalized)):
            yield from _walk(exported.get(key), normalized.get(key), f"{path}/{key}")
        return
    if (
        isinstance(exported, Sequence)
        and isinstance(normalized, Sequence)
        and not isinstance(exported, str)
        and not isinstance(normalized, str)
    ):
        for index in range(max(len(exported), len(normalized))):
            item = exported[index] if index < len(exported) else None
            other = normalized[index] if index < len(normalized) else None
            yield from _walk(item, other, f"{path}[{index}]")
        return
    if repr(exported) == repr(normalized):
        return
    yield FieldDivergence(
        path=path or "/",
        exported=repr(exported),
        normalized=repr(normalized),
        numeric_spelling_only=numerically_equal(exported, normalized),
    )


def numerically_equal(left: object, right: object) -> bool:
    """True when both are numbers of equal value, e.g. ``1`` and ``1.0``.

    ``bool`` is excluded deliberately: Python makes ``True == 1`` true, and
    reporting a boolean field as a number-spelling difference would send a
    reader looking for a serializer bug that is not there.
    """
    if isinstance(left, bool) or isinstance(right, bool):
        return False
    if not isinstance(left, int | float) or not isinstance(right, int | float):
        return False
    return math.isfinite(left) and math.isfinite(right) and left == right


def check_encoding(trace: ArenaTrace, exported_state: Mapping[str, object]) -> EncodingCheck:
    """Hash the export's own initial state both ways and compare.

    ``exported_state`` is the raw JSON object as it arrived, not the parsed
    model: the whole point is to observe the spelling the other engine chose
    before our models normalize it away.
    """
    normalized = trace.initial_state.model_dump(by_alias=True, mode="json")
    exported_hash = state_hash(dict(exported_state))
    normalized_hash = state_hash(normalized)
    if exported_hash == normalized_hash:
        return EncodingCheck(
            exported_hash=exported_hash, normalized_hash=normalized_hash, agrees=True
        )
    return EncodingCheck(
        exported_hash=exported_hash,
        normalized_hash=normalized_hash,
        agrees=False,
        divergences=tuple(_walk(exported_state, normalized)),
    )


def check_parity(payload: Mapping[str, object]) -> ParityReport:
    """Report whether this engine can grade ``payload`` as recorded.

    The stages are ordered so that no result rests on an earlier one that did
    not hold: a contract mismatch stops before hashing, and an encoding
    divergence stops before replay. Reporting a tick-by-tick comparison under
    a digest scheme known to disagree would be noise dressed as evidence.
    """
    trace = load_trace(payload)
    exported_state = payload.get("initialState")
    if not isinstance(exported_state, Mapping):
        raise TraceLoadError("initialState is missing or not an object")

    if trace.contract_version != STATE_VERSION or trace.env_version != ENV_VERSION:
        return ParityReport(
            verdict="contract_mismatch",
            blocked_reason=(
                f"export declares contract {trace.contract_version} / env "
                f"{trace.env_version}; this engine is {STATE_VERSION} / {ENV_VERSION}. "
                f"States are not known to mean the same thing, so nothing is compared."
            ),
            episode_id=trace.episode_id,
            trace_env_version=trace.env_version,
            trace_contract_version=trace.contract_version,
        )

    encoding = check_encoding(trace, exported_state)
    if not encoding.agrees:
        spelling = sum(1 for d in encoding.divergences if d.numeric_spelling_only)
        content = len(encoding.divergences) - spelling
        return ParityReport(
            verdict="encoding_divergence",
            encoding=encoding,
            blocked_reason=(
                f"the two engines hash the same restaurant differently: "
                f"{spelling} field(s) differ in number spelling only, "
                f"{content} field(s) differ in content. Every per-tick hash "
                f"comparison would fail for this reason alone, so none was run."
            ),
            episode_id=trace.episode_id,
            trace_env_version=trace.env_version,
            trace_contract_version=trace.contract_version,
        )

    return ParityReport(
        verdict="parity_unproven",
        encoding=encoding,
        replay_attempted=False,
        blocked_reason=(
            "hashes are comparable, but a trace carries no event schedule: the "
            "incidents that fire during an episode (oven failure, shelf overheat, "
            "player correction) are constructor state, not trace events. Replaying "
            "the actions without them would diverge for a missing-input reason and "
            "misreport it as an engine disagreement. Semantic parity is untested."
        ),
        episode_id=trace.episode_id,
        trace_env_version=trace.env_version,
        trace_contract_version=trace.contract_version,
    )
