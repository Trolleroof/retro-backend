"""Assemble the portable evidence packet the demo is shown from (#80).

A run bundle (`cli.write_bundle`) is evidence for *one* run. This module is the
layer above it: it takes a frozen plan -- the matrix #81 will fill -- walks the
run bundles that exist for it, and lands a directory an unfamiliar reader can
open with no provider key, no network, and no checkout of this repository's
history.

Three properties shape everything here.

**The packet is buildable before the runs exist.** The plan is frozen first and
the rows arrive later, so a row with no bundle is a first-class state, not an
error: it renders as ``planned`` with the command that would produce it. That
is what lets the packaging, the report shell, the limitations, and the
sanitisation split be reviewed while #81 is still running the matrix.

**Nothing is edited to pass.** Sanitised artifacts are copied verbatim, their
digests recorded on both sides of the copy, and every complete row is re-graded
from the *sanitised* trajectory and compared against the grade the run
recorded. A row whose copy no longer reproduces its own grade is reported as
such rather than quietly dropped.

**Shareable and private are different trees.** ``sanitized/`` is what leaves
the machine. ``attempts.jsonl`` -- every raw model reply, verbatim, with the
served model id and per-attempt provider metadata -- does not go with it; the
private index records where it lives and what it hashes to, so a reviewer with
access to the source can still tie a shared row back to its raw log.
"""

import hashlib
import json
import re
import shutil
import subprocess
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, replace
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, ValidationError
from pydantic.alias_generators import to_camel

from retro_backend.esim.case import ENV_VERSION
from retro_backend.esim.grader import GRADER_VERSION, grade
from retro_backend.esim.managed import MANAGED_SHAPE
from retro_backend.esim.trajectory import Trajectory

# Bumped when the packet's tree shape or field meanings change, so a reader can
# tell a v1 packet from a later one without inferring it from the contents.
PACKET_VERSION = "esim-evidence-packet/v1"
# The plan is a separate contract from the packet it produces: the plan is
# authored and frozen by hand, the packet is generated from it.
PLAN_VERSION = "esim-packet-plan/v1"

# What travels. Everything the grader reads, plus the manifest that says how
# the run was configured and the directive log the founder check walks.
SANITIZED_FILES = (
    "manifest.json",
    "initial-state.json",
    "final-state.json",
    "trajectory.json",
    "grade.json",
    "summary.json",
    "directives.json",
)
# Without these a row cannot be re-graded or configuration-checked, so their
# absence makes the row incomplete rather than merely thinner.
REQUIRED_FILES = (
    "manifest.json",
    "trajectory.json",
    "grade.json",
    "summary.json",
)
# What stays. The raw attempt log is the run's most useful artifact and its
# least shareable one: it carries every model reply verbatim, including the
# malformed ones, and the provider metadata attached to each.
PRIVATE_FILES = ("attempts.jsonl",)

# Defence in depth, not the primary control: no artifact this harness writes
# should contain a credential, and a hit here means one leaked into a run
# record and the run bundle itself needs looking at.
_REDACTIONS: tuple[tuple[re.Pattern[str], str], ...] = (
    (re.compile(r"nvapi-[A-Za-z0-9_\-]{8,}"), "[redacted-api-key]"),
    (re.compile(r"(?i)bearer\s+[A-Za-z0-9._\-]{8,}"), "Bearer [redacted]"),
    (re.compile(r"(?i)\b(sk|xai|hf)-[A-Za-z0-9._\-]{12,}"), "[redacted-api-key]"),
    (re.compile(r"://[^/\s:@\"]+:[^/\s@\"]+@"), "://[redacted-credentials]@"),
)

RowStatus = Literal["planned", "missing", "incomplete", "invalid", "complete"]


class PacketError(Exception):
    """A plan could not be read, or a packet could not be assembled."""


class PacketExistsError(PacketError):
    """An output directory already holds a packet."""


class _PacketModel(BaseModel):
    model_config = ConfigDict(
        alias_generator=to_camel, populate_by_name=True, extra="forbid", frozen=True
    )


class ExcludedModel(_PacketModel):
    """A model deliberately not in the matrix, and why.

    Recorded rather than dropped: "excluded" and "never considered" are
    different claims, and a roster that hides the first reads as the second.
    """

    model: str
    reason: str


class PacketPlan(_PacketModel):
    """The frozen matrix a packet is built against.

    ``bundles`` is the only field #81 is expected to add to: it maps a row id
    to the directory that row's run landed in, relative to ``--runs-root``. A
    row absent from it has not been run, which is a state the report prints
    rather than an omission it hides.
    """

    plan_version: Literal["esim-packet-plan/v1"] = PLAN_VERSION
    packet_id: str
    # "skeleton" says the matrix has not been run and the report is a shell;
    # "frozen" is the claim that these rows are the ones being shown.
    status: Literal["skeleton", "frozen"] = "skeleton"
    frozen_at: str | None = None
    shape: Literal["managed"] = "managed"
    worker_model: str
    rounds: int = Field(gt=0)
    levels: tuple[str, ...] = Field(min_length=1)
    manager_models: tuple[str, ...] = Field(min_length=1)
    excluded_models: tuple[ExcludedModel, ...] = ()
    bundles: Mapping[str, str] = Field(default_factory=dict)
    notes: tuple[str, ...] = ()


@dataclass(frozen=True)
class RegradeCheck:
    """Whether a sanitised trajectory still produces the grade it shipped with."""

    matches: bool
    detail: str


@dataclass(frozen=True)
class Row:
    """One planned cell of the matrix, and whatever evidence exists for it."""

    row_id: str
    case_id: str
    manager_model: str
    worker_model: str
    status: RowStatus
    source: Path | None = None
    missing_files: tuple[str, ...] = ()
    manifest: Mapping[str, Any] | None = None
    summary: Mapping[str, Any] | None = None
    graded: Mapping[str, Any] | None = None
    regrade: RegradeCheck | None = None
    files: tuple[Mapping[str, Any], ...] = ()
    private: tuple[Mapping[str, Any], ...] = ()
    redactions: int = 0
    note: str = ""

    @property
    def run_validity(self) -> str:
        if self.summary is None:
            return "unknown"
        return str(self.summary.get("runValidity", "unknown"))

    @property
    def regrade_command(self) -> str:
        """The command that reproduces this row's grade with no provider access."""
        return (
            "uv run python -m retro_backend.esim.cli regrade"
            f" --trajectory runs/{self.row_id}/trajectory.json"
        )


def slugify(model: str) -> str:
    """A row-id-safe form of a provider spec (``nim:openai/gpt-oss-20b``)."""
    return re.sub(r"[^a-z0-9]+", "-", model.lower()).strip("-")


def row_id(case_id: str, manager_model: str) -> str:
    return f"{case_id}--{slugify(manager_model)}"


def load_plan(path: Path) -> PacketPlan:
    """Read a packet plan, reporting every failure against the path.

    The plan is hand-authored, so a typo in a level or model name is the
    expected failure here and it has to name the file it came from.
    """
    try:
        payload = json.loads(path.read_text())
    except OSError as exc:
        raise PacketError(f"{path}: {exc.strerror or exc}") from exc
    except json.JSONDecodeError as exc:
        raise PacketError(f"{path}: not valid JSON ({exc.msg} at line {exc.lineno})") from exc
    try:
        return PacketPlan.model_validate(payload)
    except ValidationError as exc:
        raise PacketError(
            f"{path}: does not match {PLAN_VERSION} ({exc.error_count()} field error(s))\n{exc}"
        ) from exc


def sha256_file(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def redact(text: str) -> tuple[str, int]:
    """Strip anything credential-shaped, returning the text and the hit count."""
    hits = 0
    for pattern, replacement in _REDACTIONS:
        text, count = pattern.subn(replacement, text)
        hits += count
    return text, hits


def _read_json(path: Path) -> Mapping[str, Any] | None:
    """Best-effort read of a bundle artifact; ``None`` when it is unusable.

    A row with an unreadable artifact is reported as incomplete, not raised
    on: one damaged bundle in a matrix of eighteen must not stop the other
    seventeen from being packaged.
    """
    try:
        payload = json.loads(path.read_text())
    except (OSError, json.JSONDecodeError):
        return None
    return payload if isinstance(payload, dict) else None


def _regrade(trajectory_path: Path, recorded: Mapping[str, Any] | None) -> RegradeCheck:
    """Re-score a sanitised trajectory and compare it to the recorded grade.

    This is the acceptance criterion executed rather than asserted: if the
    copy that ships cannot reproduce the number printed beside it, the packet
    says so on the row instead of the reader discovering it at the demo.
    """
    try:
        payload = json.loads(trajectory_path.read_text())
        trajectory = Trajectory.model_validate(payload)
    except (OSError, json.JSONDecodeError) as exc:
        return RegradeCheck(False, f"trajectory unreadable: {exc}")
    except ValidationError as exc:
        return RegradeCheck(False, f"trajectory does not match this build ({exc.error_count()} field error(s))")
    fresh = grade(trajectory).model_dump(by_alias=True, mode="json")
    if recorded is None:
        return RegradeCheck(False, "no recorded grade to compare against")
    compared = ("outcome", "behavior", "termination", "ticks", "safetyFailure", "graderVersion")
    differences = [key for key in compared if fresh.get(key) != recorded.get(key)]
    if differences:
        return RegradeCheck(False, "differs from recorded grade: " + ", ".join(differences))
    return RegradeCheck(True, f"reproduced under {fresh.get('graderVersion')}")


def _copy_sanitized(source: Path, destination: Path) -> tuple[list[dict[str, Any]], int]:
    """Copy the shareable artifacts, recording a digest on each side of the copy.

    Both digests are kept even though they are equal whenever nothing was
    redacted. That equality is the check: a reader comparing ``sourceSha256``
    against the private index proves the shared copy is the run's own file,
    and a differing ``sanitizedSha256`` is exactly where a redaction happened.
    """
    destination.mkdir(parents=True, exist_ok=True)
    records: list[dict[str, Any]] = []
    redactions = 0
    for name in SANITIZED_FILES:
        origin = source / name
        if not origin.exists():
            continue
        raw = origin.read_text()
        cleaned, hits = redact(raw)
        redactions += hits
        target = destination / name
        target.write_text(cleaned)
        records.append(
            {
                "file": name,
                "bytes": target.stat().st_size,
                "sourceSha256": sha256_file(origin),
                "sanitizedSha256": sha256_file(target),
                "redactions": hits,
            }
        )
    return records, redactions


def _private_records(source: Path) -> list[dict[str, Any]]:
    """Index the artifacts that stay behind, by absolute path and digest."""
    records: list[dict[str, Any]] = []
    for name in PRIVATE_FILES:
        origin = source / name
        if not origin.exists():
            continue
        records.append(
            {
                "file": name,
                "path": str(origin.resolve()),
                "bytes": origin.stat().st_size,
                "sha256": sha256_file(origin),
                "withheldBecause": "raw model replies and per-attempt provider metadata",
            }
        )
    return records


def collect_row(
    *,
    case_id: str,
    manager_model: str,
    plan: PacketPlan,
    runs_root: Path,
    sanitized_runs: Path,
) -> Row:
    """Resolve one planned cell against the bundles on disk and copy what exists."""
    identifier = row_id(case_id, manager_model)
    base = Row(
        row_id=identifier,
        case_id=case_id,
        manager_model=manager_model,
        worker_model=plan.worker_model,
        status="planned",
    )
    recorded = plan.bundles.get(identifier)
    if recorded is None:
        return base
    source = runs_root / recorded
    if not source.is_dir():
        return replace(base, status="missing", source=source, note=f"no bundle at {source}")

    missing = tuple(name for name in REQUIRED_FILES if not (source / name).exists())
    files, redactions = _copy_sanitized(source, sanitized_runs / identifier)
    private = tuple(_private_records(source))
    manifest = _read_json(source / "manifest.json")
    summary = _read_json(source / "summary.json")
    graded = _read_json(source / "grade.json")
    unreadable = tuple(
        name
        for name, payload in (
            ("manifest.json", manifest),
            ("summary.json", summary),
            ("grade.json", graded),
        )
        if payload is None and name not in missing
    )
    missing = tuple(sorted({*missing, *unreadable}))

    regrade = None
    if not missing:
        regrade = _regrade(sanitized_runs / identifier / "trajectory.json", graded)

    if missing:
        status: RowStatus = "incomplete"
        note = "bundle is missing or unreadable: " + ", ".join(missing)
    elif summary is not None and summary.get("runValidity") not in (None, "valid"):
        status = "invalid"
        note = f"run recorded runValidity={summary.get('runValidity')}"
    elif regrade is not None and not regrade.matches:
        status = "invalid"
        note = f"offline re-grade failed: {regrade.detail}"
    else:
        status = "complete"
        note = ""

    return Row(
        row_id=identifier,
        case_id=case_id,
        manager_model=manager_model,
        worker_model=plan.worker_model,
        status=status,
        source=source,
        missing_files=missing,
        manifest=manifest,
        summary=summary,
        graded=graded,
        regrade=regrade,
        files=tuple(files),
        private=private,
        redactions=redactions,
        note=note,
    )


def collect_rows(plan: PacketPlan, *, runs_root: Path, sanitized_runs: Path) -> list[Row]:
    """Expand the matrix in a fixed order: level outer, manager inner.

    The order is the plan's own, not sorted: a reader comparing two rows on the
    same level should find them adjacent, and the plan author controls which
    level leads.
    """
    return [
        collect_row(
            case_id=case_id,
            manager_model=manager_model,
            plan=plan,
            runs_root=runs_root,
            sanitized_runs=sanitized_runs,
        )
        for case_id in plan.levels
        for manager_model in plan.manager_models
    ]


def counts(rows: Sequence[Row]) -> dict[str, int]:
    """Planned / completed / failed counts, with the failure kinds kept apart.

    #81 asks for infrastructure errors and invalid runs to be distinguishable
    from manager failures, and this is the packet's half of that: a row is
    ``invalid`` because the run recorded itself degraded or because its
    evidence no longer reproduces -- neither is a statement about the model.
    """
    tally = {status: 0 for status in ("planned", "missing", "incomplete", "invalid", "complete")}
    for row in rows:
        tally[row.status] += 1
    return {
        "planned": len(rows),
        "completed": tally["complete"],
        "invalid": tally["invalid"],
        "incomplete": tally["incomplete"],
        "missingBundle": tally["missing"],
        "notYetRun": tally["planned"],
    }


def git_revision(repo: Path) -> str | None:
    """The revision the packet was cut at, or ``None`` outside a checkout."""
    try:
        result = subprocess.run(
            ["git", "-C", str(repo), "rev-parse", "HEAD"],
            capture_output=True,
            text=True,
            check=False,
        )
    except OSError:
        return None
    return result.stdout.strip() or None


def build_bundle_manifest(
    plan: PacketPlan, rows: Sequence[Row], *, revision: str | None, built_at: str
) -> dict[str, Any]:
    """The packet's own manifest: what is inside, at which revision, hashing to what."""
    return {
        "packetVersion": PACKET_VERSION,
        "packetId": plan.packet_id,
        "status": plan.status,
        "builtAt": built_at,
        "frozenAt": plan.frozen_at,
        "repositoryRevision": revision,
        "envVersion": ENV_VERSION,
        "graderVersion": GRADER_VERSION,
        "harness": {"shape": MANAGED_SHAPE, "rounds": plan.rounds},
        "workerModel": plan.worker_model,
        "managerModels": list(plan.manager_models),
        "levels": list(plan.levels),
        "excludedModels": [item.model_dump(by_alias=True) for item in plan.excluded_models],
        "counts": counts(rows),
        "withheldFromSanitizedBundle": list(PRIVATE_FILES),
        "rows": [
            {
                "rowId": row.row_id,
                "caseId": row.case_id,
                "managerModel": row.manager_model,
                "workerModel": row.worker_model,
                "status": row.status,
                "runValidity": row.run_validity,
                "regradeMatches": None if row.regrade is None else row.regrade.matches,
                "regradeDetail": None if row.regrade is None else row.regrade.detail,
                "regradeCommand": row.regrade_command,
                # Cost is not derivable from anything the run records, and a
                # zero here would read as "free and measured" rather than
                # "unmeasured". See LIMITATIONS.md.
                "costUsd": None,
                "costBasis": "unknown; free developer tier, no per-model rate confirmed",
                "artifacts": [dict(record) for record in row.files],
                "missingFiles": list(row.missing_files),
                "note": row.note,
            }
            for row in rows
        ],
        "notes": list(plan.notes),
    }


def build_private_index(
    plan: PacketPlan, rows: Sequence[Row], *, runs_root: Path, built_at: str
) -> dict[str, Any]:
    """Where each shared row came from, for a reviewer who has the source machine.

    This file is written **outside** ``sanitized/`` and is the reason the split
    is safe to share: it is the only place absolute paths and the raw attempt
    logs' digests appear.
    """
    return {
        "packetVersion": PACKET_VERSION,
        "packetId": plan.packet_id,
        "builtAt": built_at,
        "runsRoot": str(runs_root.resolve()),
        "withheld": list(PRIVATE_FILES),
        "rows": [
            {
                "rowId": row.row_id,
                "sourceBundle": None if row.source is None else str(row.source.resolve()),
                "status": row.status,
                "sharedArtifacts": [
                    {"file": record["file"], "sourceSha256": record["sourceSha256"]}
                    for record in row.files
                ],
                "privateArtifacts": [dict(record) for record in row.private],
            }
            for row in rows
        ],
    }


def _score(payload: Mapping[str, Any] | None, block: str, key: str) -> str:
    """One graded number as text, or ``--`` when the row has no grade yet."""
    if payload is None:
        return "--"
    value = payload.get(block, {})
    if not isinstance(value, Mapping):
        return "--"
    found = value.get(key)
    return "--" if found is None else f"{found}"


def _number(payload: Mapping[str, Any] | None, *path: str) -> str:
    """A nested summary figure as text, or ``--`` when it was never recorded."""
    current: Any = payload
    for key in path:
        if not isinstance(current, Mapping):
            return "--"
        current = current.get(key)
    return "--" if current is None else f"{current}"


def render_task_card(plan: PacketPlan, rows: Sequence[Row], *, revision: str | None) -> str:
    """The one-page card: what is frozen, what is being compared, what it claims.

    This is the packet's own card, not a copy of the manifest: the manifest
    freezes the environment and budgets, and this freezes *the matrix* --
    which models, on which levels, at which repository revision, with which
    rows still outstanding.
    """
    tally = counts(rows)
    lines = [
        f"# {plan.packet_id} — task card",
        "",
        f"Packet: `{PACKET_VERSION}` · status **{plan.status}**"
        + (f" · frozen {plan.frozen_at}" if plan.frozen_at else " · not yet frozen"),
        f"Repository revision: `{revision or 'unknown'}`",
        f"Environment: `{ENV_VERSION}` · grader: `{GRADER_VERSION}`",
        "",
        "## What is being compared",
        "",
        f"Shape: {MANAGED_SHAPE}. One **manager** model runs the shift by posting",
        "standing directives; a separate, **fixed** worker model makes all four worker",
        "decisions under them. Manager identity is what varies between rows.",
        "",
        f"- Fixed worker model: `{plan.worker_model}`",
        f"- Rounds per level: {plan.rounds}",
        f"- Levels ({len(plan.levels)}): " + ", ".join(f"`{level}`" for level in plan.levels),
        f"- Manager models ({len(plan.manager_models)}): "
        + ", ".join(f"`{model}`" for model in plan.manager_models),
        f"- Matrix: {len(plan.levels)} levels x {len(plan.manager_models)} managers"
        + f" = {tally['planned']} rows, one run each",
        "",
        "The claim this supports is **manager-model comparison with worker identity",
        "held fixed** — nothing about any single role, and nothing about a mixed-model",
        "shift.",
        "",
        "## Roster exclusions",
        "",
    ]
    if plan.excluded_models:
        lines += ["| Model | Why excluded |", "| --- | --- |"]
        lines += [f"| `{item.model}` | {item.reason} |" for item in plan.excluded_models]
    else:
        lines.append("None recorded.")
    lines += [
        "",
        "## Run counts",
        "",
        "| Planned | Completed | Invalid | Incomplete | Bundle missing | Not yet run |",
        "| --- | --- | --- | --- | --- | --- |",
        f"| {tally['planned']} | {tally['completed']} | {tally['invalid']} |"
        + f" {tally['incomplete']} | {tally['missingBundle']} | {tally['notYetRun']} |",
        "",
        "## What travels with this packet",
        "",
        "| File | Contents |",
        "| --- | --- |",
        "| `TASK-CARD.md` | this card |",
        "| `REPORT.md` | one row per planned run, each linked to its evidence |",
        "| `LIMITATIONS.md` | what these numbers do and do not establish |",
        "| `bundle-manifest.json` | frozen configuration, counts, per-file digests |",
        "| `runs/<row>/` | manifest, states, trajectory, grade, summary, directives |",
        "",
        "`attempts.jsonl` — every raw model reply — is **not** in this tree; see",
        "`LIMITATIONS.md` §Sanitised and private.",
        "",
    ]
    return "\n".join(lines) + "\n"


def render_report(plan: PacketPlan, rows: Sequence[Row], *, built_at: str) -> str:
    """The static report: every advertised row, resolving to its own evidence.

    Rows that have not been run are printed, not omitted. A matrix that shows
    only its completed cells is a different and more flattering claim than the
    one this packet makes, and #81's report replaces these placeholders in
    place — the row ids, commands, and artifact paths do not move when it does.
    """
    tally = counts(rows)
    pending = tally["planned"] - tally["completed"]
    lines = [
        f"# {plan.packet_id} — manager-model report",
        "",
        f"Built {built_at} · packet `{PACKET_VERSION}` · status **{plan.status}**",
        "",
    ]
    if pending:
        lines += [
            f"> **Placeholder report.** {tally['completed']} of {tally['planned']} planned rows"
            + " have evidence in this packet. The remaining rows are printed with the exact"
            + " command that produces them and resolve once retro-backend#81 lands its matrix."
            + " No row below is an estimate: a cell with no run shows `--`, never a number.",
            "",
        ]
    lines += [
        f"Fixed worker model `{plan.worker_model}` · {plan.rounds} rounds per level ·"
        + " one run per model per level.",
        "",
        "## Matrix",
        "",
        "| Row | Level | Manager | Status | Validity | Safety | Completion | Wait | Waste |"
        + " Satisfaction | Coordination | Evidence |",
        "| --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- |",
    ]
    for row in rows:
        evidence = (
            f"[`runs/{row.row_id}/`](runs/{row.row_id}/)"
            if row.status in ("complete", "invalid", "incomplete")
            else "not yet run"
        )
        lines.append(
            f"| `{row.row_id}` | `{row.case_id}` | `{row.manager_model}` | {row.status} |"
            + f" {row.run_validity} |"
            + f" {_score(row.graded, 'outcome', 'safety')} |"
            + f" {_score(row.graded, 'outcome', 'orderCompletion')} |"
            + f" {_score(row.graded, 'outcome', 'waitTime')} |"
            + f" {_score(row.graded, 'outcome', 'foodWaste')} |"
            + f" {_score(row.graded, 'outcome', 'customerSatisfaction')} |"
            + f" {_score(row.graded, 'behavior', 'coordination')} |"
            + f" {evidence} |"
        )
    lines += [
        "",
        "## Usage and cost",
        "",
        "Latency is reported beside queue delay on purpose: a run throttled by this"
        + " process' own share of the rate-limit budget must not read as a run against a"
        + " slow model.",
        "",
        "| Row | Manager turns | Directives | Worker turns | Invalid | Model errors |"
        + " Turns lost | Input tokens | Output tokens | Latency ms | Queue ms | Cost |",
        "| --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- |",
    ]
    for row in rows:
        summary = row.summary
        lines.append(
            f"| `{row.row_id}` |"
            + f" {_number(summary, 'manager', 'turns')} |"
            + f" {_number(summary, 'manager', 'directivesAccepted')} |"
            + f" {_number(summary, 'workers', 'turns')} |"
            + f" {_number(summary, 'workers', 'invalidResponses')} |"
            + f" {_number(summary, 'manager', 'modelErrors')}/"
            + f"{_number(summary, 'workers', 'modelErrors')} |"
            + f" {_number(summary, 'manager', 'turnsLostToProvider')}/"
            + f"{_number(summary, 'workers', 'turnsLostToProvider')} |"
            + f" {_number(summary, 'manager', 'inputTokens')} |"
            + f" {_number(summary, 'manager', 'outputTokens')} |"
            + f" {_number(summary, 'manager', 'latencyMs')} |"
            + f" {_number(summary, 'manager', 'queueDelayMs')} |"
            + " unknown |"
        )
    lines += [
        "",
        "Cost is `unknown` for every row and will stay that way unless an authoritative"
        + " per-model rate is confirmed: these runs fall inside NVIDIA NIM's free developer"
        + " tier, which reports no billed figure. A `0` here would read as measured-and-free.",
        "",
        "## Rows",
        "",
    ]
    for row in rows:
        lines += [
            f"### `{row.row_id}`",
            "",
            f"- Level: `{row.case_id}`",
            f"- Manager: `{row.manager_model}` · worker: `{row.worker_model}`",
            f"- Status: **{row.status}**" + (f" — {row.note}" if row.note else ""),
        ]
        if row.status == "planned":
            lines += [
                "- Produced by:",
                "",
                "```bash",
                "uv run python -m retro_backend.esim.cli run-managed \\",
                f"  --case {row.case_id} \\",
                f"  --manager-model {row.manager_model} \\",
                f"  --worker-model {row.worker_model} \\",
                f"  --rounds {plan.rounds} \\",
                f"  --output-dir runs/{row.row_id}",
                "```",
                "",
            ]
            continue
        regrade = row.regrade
        lines += [
            "- Configuration: [`manifest.json`](runs/" + row.row_id + "/manifest.json)",
            "- Decisions: [`trajectory.json`](runs/" + row.row_id + "/trajectory.json)"
            + " · [`directives.json`](runs/" + row.row_id + "/directives.json)",
            "- Grade: [`grade.json`](runs/" + row.row_id + "/grade.json)"
            + " · summary: [`summary.json`](runs/" + row.row_id + "/summary.json)",
            "- Offline re-grade: "
            + (
                "not checked"
                if regrade is None
                else ("reproduces" if regrade.matches else "**does not reproduce**")
                + f" — {regrade.detail}"
            ),
            "",
            "```bash",
            row.regrade_command,
            "```",
            "",
        ]
    lines += [
        "## Reproducing a grade with no provider access",
        "",
        "Every command above runs against files in this packet only. `regrade` calls no",
        "model and steps no simulation: the grader is a pure function of the recorded",
        "trajectory, so the number it prints is the number in `grade.json` or the packet",
        "is wrong and says so on the row.",
        "",
        "```bash",
        "uv sync --frozen",
        f"uv run python -m retro_backend.esim.cli regrade --trajectory runs/{rows[0].row_id if rows else '<row>'}/trajectory.json",
        "```",
        "",
    ]
    return "\n".join(lines) + "\n"


def render_limitations(plan: PacketPlan, rows: Sequence[Row]) -> str:
    """What these numbers do not establish, stated where the numbers are read.

    #80 asks for the limits to travel with the evidence rather than live in a
    document a reader has to go find, so this is generated into the packet and
    reflects the packet's own counts.
    """
    tally = counts(rows)
    return "\n".join(
        [
            f"# {plan.packet_id} — limitations",
            "",
            "Read this beside `REPORT.md`. Every item is a limit on what the rows in that",
            "report support, not a caveat about the harness's condition.",
            "",
            "## One run per model per level",
            "",
            f"Each of the {tally['planned']} rows is a single run. The same configuration",
            "produces controlled fairness, not deterministic output: the worker model is",
            "sampled live, and run-to-run variance on the dangerous variant has already been",
            "observed large enough to flip a safety verdict. Close results are inconclusive,",
            "ties are published as ties, and no stable ranking is claimed from one run.",
            "",
            "## Live worker variation",
            "",
            f"The worker model `{plan.worker_model}` is held fixed by identity, not by",
            "output. Two managers on the same level do not receive identical worker",
            "responses, so a difference between rows is a difference between shifts, and is",
            "only attributed to a manager decision where a directive and the response under",
            "it are both in the trajectory.",
            "",
            "## Verified roster and coverage",
            "",
            "The roster is what this account can actually call, verified by probe, not the",
            "provider's catalog. Models that are unreachable or that cannot emit parseable",
            "tool JSON are recorded as exclusions on the task card rather than dropped. The",
            "levels are the three calibrated ones; nothing here covers a mixed-model shift,",
            "an isolated role measurement, or a level outside that set.",
            "",
            "## Free-prototype constraints",
            "",
            "These runs are made on a free developer tier, under a rate-limit budget this",
            "process shares with the demo app, with no per-model price confirmed against an",
            "authoritative source. Cost is reported as `unknown`, never as `0`. Provider-side",
            "revision pinning is not available: the model that answered is recorded from the",
            "response, and a provider may serve a snapshot it does not name.",
            "",
            "## No equivalence or improvement claim",
            "",
            "This is not a professional or APEX-equivalent evaluation, and it establishes",
            "nothing about training, fine-tuning, or improvement of any model. It is an",
            "exploratory comparison of manager behavior in one frozen restaurant harness.",
            "",
            "## Sanitised and private",
            "",
            "`sanitized/` is the shareable tree and holds everything a grade is reproduced",
            "from. It deliberately omits `attempts.jsonl`, the raw per-attempt log of every",
            "model reply including malformed ones, together with the provider metadata",
            "attached to each. Those stay on the source machine and are indexed by absolute",
            "path and SHA-256 in `private/source-index.json`, so a reviewer with access can",
            "confirm that a shared artifact is byte-for-byte the run's own file. Digests are",
            "recorded on both sides of the copy: `sourceSha256` differs from",
            "`sanitizedSha256` only where a redaction fired, and no such difference is",
            "expected — a run record should never contain a credential in the first place.",
            "",
            "## Recorded evidence, not a live session",
            "",
            "A packet is recorded evidence. It shows what these runs did when they were",
            "made; it is not a live demonstration, and a re-grade reproducing a score is a",
            "statement about the grader and the recording, not a re-execution of the shift.",
            "",
        ]
    )


def write_packet(
    plan: PacketPlan,
    *,
    runs_root: Path,
    out_dir: Path,
    repo: Path,
    overwrite: bool = False,
) -> dict[str, Any]:
    """Assemble the whole packet and return its bundle manifest.

    The two trees are written together and only together: a `sanitized/` tree
    with no `private/source-index.json` beside it has lost the mapping back to
    the runs it was cut from, which is the half of the split that makes the
    other half auditable.
    """
    sanitized = out_dir / "sanitized"
    private = out_dir / "private"
    guard_packet_dir(sanitized, overwrite=overwrite)
    # A rebuild replaces the report wholesale, so the run tree has to go with
    # it: a row dropped from the plan would otherwise survive as an orphaned
    # directory the new report never mentions.
    if overwrite and (sanitized / "runs").exists():
        shutil.rmtree(sanitized / "runs")

    built_at = datetime.now(UTC).isoformat()
    revision = git_revision(repo)
    rows = collect_rows(plan, runs_root=runs_root, sanitized_runs=sanitized / "runs")

    manifest = build_bundle_manifest(plan, rows, revision=revision, built_at=built_at)
    sanitized.mkdir(parents=True, exist_ok=True)
    private.mkdir(parents=True, exist_ok=True)
    (sanitized / "bundle-manifest.json").write_text(
        json.dumps(manifest, indent=1, sort_keys=True) + "\n"
    )
    (sanitized / "TASK-CARD.md").write_text(render_task_card(plan, rows, revision=revision))
    (sanitized / "REPORT.md").write_text(render_report(plan, rows, built_at=built_at))
    (sanitized / "LIMITATIONS.md").write_text(render_limitations(plan, rows))
    (private / "source-index.json").write_text(
        json.dumps(
            build_private_index(plan, rows, runs_root=runs_root, built_at=built_at),
            indent=1,
            sort_keys=True,
        )
        + "\n"
    )
    return manifest


PACKET_FILES = ("TASK-CARD.md", "REPORT.md", "LIMITATIONS.md", "bundle-manifest.json")


def guard_packet_dir(sanitized: Path, *, overwrite: bool) -> None:
    """Refuse to write a second packet over an existing one.

    Same reasoning as a run bundle: the files are written under fixed names, so
    a rebuild into an occupied directory replaces some artifacts and leaves
    others -- a packet whose report describes rows its `runs/` tree no longer
    holds.
    """
    if overwrite:
        return
    existing = [name for name in PACKET_FILES if (sanitized / name).exists()]
    if existing:
        found = ", ".join(sorted(existing))
        raise PacketExistsError(
            f"{sanitized} already holds a packet ({found}). "
            + "Choose a different --output-dir, or pass --overwrite to replace it."
        )
