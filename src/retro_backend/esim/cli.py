"""Run a frozen E-Sim case against one model and write an evidence bundle (#75).

This is the thin layer the benchmark is driven through. It owns no simulation,
no scoring, and no provider logic: it resolves a case, hands it to
:func:`~retro_backend.esim.runner.run_model_case`, passes the resulting
trajectory to :func:`~retro_backend.esim.grader.grade`, and lands both on disk
beside the manifest that says how the run was configured.

**One model per run, all four roles.** The selected model answers as chef,
server, host, and expediter in the fixed order :data:`ROLE_ORDER` sets. The
comparison this supports is *between models in a fixed multi-role harness*; it
is not an isolated test of one role, and it is not a single-manager benchmark.

**The reference policy is deliberately not reachable from here.** ``policies``
is infrastructure-only -- nothing under ``src/`` may import it -- so that no
scripted action can ever be recorded as a model decision. The offline
reference rollout lives in ``scripts/reference_rollout.py`` instead.
"""

import argparse
import asyncio
import json
import sys
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from pydantic import ValidationError

from retro_backend.esim.case import ENV_VERSION, CaseError, load_case
from retro_backend.esim.grader import GRADER_VERSION, SERVED, Grade, grade
from retro_backend.esim.parity import ParityReport, TraceLoadError, check_parity
from retro_backend.esim.runner import (
    RunnerConfig,
    RunResult,
    effective_max_turns,
    run_model_case,
)
from retro_backend.esim.state_hash import state_hash
from retro_backend.esim.trajectory import Trajectory

# Bumped when the bundle's file set or field meanings change, so a report can
# tell a v1 bundle from a later one without guessing from its contents.
BUNDLE_VERSION = "esim-run-bundle/v1"


def positive_int(value: str) -> int:
    """An argparse type that rejects zero and negatives.

    ``--repeats 0`` previously exited 0 having produced no bundle at all,
    reporting success for work that never ran.
    """
    parsed = int(value)
    if parsed < 1:
        raise argparse.ArgumentTypeError(f"must be 1 or greater, got {parsed}")
    return parsed


def resolve_case_id(value: str) -> str:
    """Accept either a case id or a path to its JSON file.

    The planning card spells this argument as a path
    (``contracts/esim/cases/dinner-rush-001.json``) while :func:`load_case`
    addresses cases by id. Taking both means the documented command works
    without teaching the loader about paths it must not follow.
    """
    if value.endswith(".json") or "/" in value or "\\" in value:
        return Path(value).stem
    return value


def _write_json(path: Path, payload: object) -> None:
    path.write_text(json.dumps(payload, indent=1, sort_keys=True) + "\n")


def build_manifest(
    config: RunnerConfig,
    *,
    case_seed: int,
    contract_version: str,
    max_ticks: int,
    repeat: int,
    repeats: int,
    started_at: str,
) -> dict[str, Any]:
    """The configuration record that travels with the run.

    Sampling is reported in two halves on purpose. ``sampling.set`` is what
    this harness chose and can defend; ``sampling.providerControlled`` is what
    the provider decides for us. Folding them together would let the bundle
    imply a determinism it does not have: a hosted model at temperature 0 is
    not bit-reproducible across days, and no seed is sent upstream.
    """
    return {
        "bundleVersion": BUNDLE_VERSION,
        "envVersion": ENV_VERSION,
        "contractVersion": contract_version,
        "graderVersion": GRADER_VERSION,
        "harness": {
            "shape": "one model operates all four worker roles",
            "roleOrder": ["chef", "server", "host", "expediter"],
            "claim": "model comparison in a fixed multi-role restaurant harness",
        },
        "caseId": config.case_id,
        "model": config.model,
        "seed": case_seed,
        "repeat": repeat,
        "repeats": repeats,
        "budgets": {
            "maxAttempts": config.max_attempts,
            "maxOutputTokens": config.max_output_tokens,
            # The enforced budget, not the raw setting: unset means the runner
            # derives it, and recording None there would describe a run under
            # a budget it never used.
            "maxTurns": effective_max_turns(config, max_ticks),
            "maxTurnsExplicitlySet": config.max_turns is not None,
        },
        "sampling": {
            "set": {"temperature": 0.0},
            # Recorded rather than claimed: the adapter sends no seed, and the
            # provider may serve a revision it does not name.
            "providerControlled": [
                "sampling seed (not sent; provider default applies)",
                "model revision / snapshot pinning (not exposed by NIM)",
            ],
        },
        "startedAt": started_at,
    }


def write_bundle(
    out_dir: Path,
    *,
    manifest: dict[str, Any],
    result: RunResult,
    graded: Grade,
) -> None:
    """Land every artifact the acceptance checklist asks a run to record."""
    out_dir.mkdir(parents=True, exist_ok=True)
    trajectory: Trajectory = result.trajectory
    initial = trajectory.initial_state.model_dump(by_alias=True, mode="json")
    final = trajectory.final_state.model_dump(by_alias=True, mode="json")

    _write_json(out_dir / "manifest.json", manifest)
    _write_json(out_dir / "initial-state.json", initial)
    _write_json(out_dir / "final-state.json", final)
    _write_json(out_dir / "trajectory.json", trajectory.model_dump(by_alias=True, mode="json"))
    _write_json(out_dir / "grade.json", graded.model_dump(by_alias=True, mode="json"))
    _write_json(
        out_dir / "summary.json",
        {
            **result.summary,
            "initialStateHash": state_hash(initial),
            "finalStateHash": trajectory.final_state_hash,
        },
    )


BUNDLE_FILES = (
    "manifest.json",
    "initial-state.json",
    "final-state.json",
    "trajectory.json",
    "grade.json",
    "summary.json",
    "attempts.jsonl",
)


class BundleExistsError(Exception):
    """An output directory already holds a run's evidence."""


def guard_output_dir(out_dir: Path, *, overwrite: bool) -> None:
    """Refuse to write a second run over an existing bundle.

    Every artifact here is written under a fixed name, so a rerun into the
    same directory replaces the previous evidence. Worse, the files are
    written at different moments: ``attempts.jsonl`` is truncated when the run
    starts and the rest are replaced only if it finishes, so a run that dies
    partway leaves a new attempt log beside the previous run's states and
    grade -- a bundle that looks complete while describing two different runs.
    """
    if overwrite:
        return
    existing = [name for name in BUNDLE_FILES if (out_dir / name).exists()]
    if existing:
        found = ", ".join(sorted(existing))
        message = (
            f"{out_dir} already holds a run ({found}). "
            f"Choose a different --output-dir, or pass --overwrite to replace it."
        )
        raise BundleExistsError(message)


async def _run_once(
    config: RunnerConfig, out_dir: Path, *, repeat: int, repeats: int, overwrite: bool
) -> tuple[Grade, Trajectory]:
    started_at = datetime.now(UTC).isoformat()
    case = load_case(config.case_id)
    out_dir.mkdir(parents=True, exist_ok=True)
    guard_output_dir(out_dir, overwrite=overwrite)
    # The per-attempt log is a stream, so it is written as the run happens
    # rather than assembled afterwards: a run that dies mid-episode still
    # leaves behind every raw model reply it did get.
    with (out_dir / "attempts.jsonl").open("w") as sink:
        result = await run_model_case(config, jsonl=sink)
    graded = grade(result.trajectory)
    write_bundle(
        out_dir,
        manifest=build_manifest(
            config,
            case_seed=case.seed,
            contract_version=case.contract_version,
            max_ticks=case.initial_state.episode.max_ticks,
            repeat=repeat,
            repeats=repeats,
            started_at=started_at,
        ),
        result=result,
        graded=graded,
    )
    return graded, result.trajectory


def _report_scores(graded: Grade) -> None:
    """The score block, shared by ``run`` and ``regrade`` so both read alike."""
    o, b = graded.outcome, graded.behavior
    print(
        f"     termination={graded.termination} ticks={graded.ticks} safetyFailure={graded.safety_failure}"
    )
    print(
        f"     safety={o.safety} completion={o.order_completion} wait={o.wait_time} waste={o.food_waste} satisfaction={o.customer_satisfaction}"
    )
    print(
        f"     coordination={b.coordination} scopeControl={b.scope_control} correctionUptake={b.correction_uptake}"
    )


def _report(graded: Grade, summary: dict[str, Any], out_dir: Path, trajectory: Trajectory) -> None:
    print(f"  -> {out_dir}")
    _report_scores(graded)
    for line in penalty_lines(graded, trajectory):
        print(line)
    if summary.get("servedModelMismatch"):
        print(f"     !! served model mismatch: {summary.get('servedModels')}")


async def _run_command(args: argparse.Namespace) -> int:
    case_id = resolve_case_id(args.case)
    # Only budgets the caller actually named are passed through. Repeating the
    # runner's defaults here once shadowed them: the CLI kept sending its own
    # stale 256-token budget after the runner's had been raised, and every run
    # went out under a budget the manifest then faithfully recorded as chosen.
    overrides: dict[str, int] = {
        name: value
        for name, value in (
            ("max_attempts", args.max_attempts),
            ("max_output_tokens", args.max_output_tokens),
            ("max_turns", args.max_turns),
        )
        if value is not None
    }
    config = RunnerConfig(case_id=case_id, model=args.model, **overrides)
    root = Path(args.output_dir)
    print(f"case={case_id} model={args.model} repeats={args.repeats}")
    for repeat in range(1, args.repeats + 1):
        out_dir = root / f"repeat-{repeat:02d}" if args.repeats > 1 else root
        print(f"[{repeat}/{args.repeats}] running...")
        graded, trajectory = await _run_once(
            config, out_dir, repeat=repeat, repeats=args.repeats, overwrite=args.overwrite
        )
        summary = json.loads((out_dir / "summary.json").read_text())
        _report(graded, summary, out_dir, trajectory)
    return 0


class TrajectoryLoadError(Exception):
    """A stored trajectory could not be read as one."""


def _read_json(path: Path) -> object:
    """Read and parse a JSON file, describing any failure against the path.

    ``OSError`` covers the whole family, not just a missing file: a directory
    passed where a bundle was meant, a permission denial, a dangling symlink.
    Catching only ``FileNotFoundError`` let those escape ``main`` as a
    traceback, so the documented exit code for unreadable input was not
    what the caller actually got.
    """
    try:
        return json.loads(path.read_text())
    except OSError as exc:
        raise _ReadError(f"{path}: {exc.strerror or exc}") from exc
    except json.JSONDecodeError as exc:
        raise _ReadError(f"{path}: not valid JSON ({exc.msg} at line {exc.lineno})") from exc


class _ReadError(Exception):
    """Internal: a path could not be read or parsed. Re-raised per command."""


def load_trajectory(path: Path) -> Trajectory:
    """Read a recorded trajectory back off disk.

    Every failure mode is reported against the path rather than raised raw:
    re-grading an archive walks over bundles this build did not write, and a
    bare ``ValidationError`` there does not say which file was unreadable.

    A parse failure is the signal the schema guard exists to catch -- a
    ``Trajectory`` field added since the bundle was written makes every
    archived run ungradeable, and this is where that surfaces.
    """
    try:
        payload = _read_json(path)
    except _ReadError as exc:
        raise TrajectoryLoadError(str(exc)) from exc
    try:
        return Trajectory.model_validate(payload)
    except ValidationError as exc:
        expected = Trajectory.model_fields["version"].default
        raise TrajectoryLoadError(
            f"{path}: does not match {expected} ({exc.error_count()} field error(s))\n{exc}"
        ) from exc


def penalty_lines(graded: Grade, trajectory: Trajectory) -> list[str]:
    """Name the tick and event behind every safety and completion penalty.

    The scores alone say a run lost points; they do not say where. This walks
    the findings the grader already produced back to the tick that caused
    them, which is what makes a re-grade actionable rather than a number.

    Blocked attempts are listed beside violations and labelled as blocked.
    A run the simulator saved is not the same as a run that chose well, and
    collapsing the two would hide exactly the distinction the safety probe
    exists to draw.
    """
    lines: list[str] = []
    for finding in graded.safety_findings:
        label = "VIOLATION" if finding.is_violation else "blocked  "
        where = f"tick={finding.tick} actor={finding.actor_id} tool={finding.tool}"
        lines.append(f"     {label} {where} kind={finding.kind}: {finding.detail}")
    if graded.outcome.order_completion < 1.0:
        unserved = [order.id for order in trajectory.final_state.orders if order.status != SERVED]
        total = len(trajectory.final_state.orders)
        named = ", ".join(unserved)
        count = f"{len(unserved)}/{total}"
        lines.append(
            f"     COMPLETION {count} orders unserved at tick {trajectory.final_tick}: {named}"
        )
    return lines


async def _regrade_command(args: argparse.Namespace) -> int:
    """Re-apply the current grader to a trajectory already on disk.

    No model is called and no simulation is stepped: :func:`grade` is a pure
    function of the trajectory, so this reproduces exactly what the run would
    score under today's rules. That is the point -- when ``GRADER_VERSION``
    moves, re-running the model would resample and grade a *different*
    trajectory, which cannot be compared against the old figure.
    """
    path = Path(args.trajectory)
    trajectory = load_trajectory(path)
    graded = grade(trajectory)

    print(f"case={graded.case_id} seed={graded.seed} grader={graded.grader_version}")
    print(f"  <- {path}")
    _report_scores(graded)
    for line in penalty_lines(graded, trajectory):
        print(line)

    if args.output is not None:
        destination = Path(args.output)
        destination.parent.mkdir(parents=True, exist_ok=True)
        _write_json(destination, graded.model_dump(by_alias=True, mode="json"))
        print(f"  -> {destination}")
    return 0


def _report_parity(report: ParityReport) -> None:
    print(f"episode={report.episode_id} verdict={report.verdict}")
    if report.ticks_compared:
        print(f"  ticks compared: {report.ticks_compared}")
    for difference in report.differences:
        print(f"  {difference}")
    print(f"  why: {report.reason}")


async def _parity_command(args: argparse.Namespace) -> int:
    """Check whether a foreign trace export is gradeable by this engine.

    Exit status is the finding: 0 means replay reproduced every compared
    tick, 5 means the export is not comparable, 6 means it is comparable but
    parity was never demonstrated -- typically because the export names no
    ``caseId``, so the event schedule cannot be recovered. An unproven check
    must not look like a pass, which is why 6 is not 0.
    """
    path = Path(args.trace)
    try:
        payload = _read_json(path)
    except _ReadError as exc:
        raise TraceLoadError(str(exc)) from exc
    if not isinstance(payload, dict):
        raise TraceLoadError(f"{path}: top level is not an object")

    report = check_parity(payload)
    print(f"  <- {path}")
    _report_parity(report)

    if args.output is not None:
        destination = Path(args.output)
        destination.parent.mkdir(parents=True, exist_ok=True)
        _write_json(destination, report.model_dump(by_alias=True, mode="json"))
        print(f"  -> {destination}")
    if report.verdict == "parity":
        return 0
    return 5 if report.verdict == "mismatch" else 6


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="python -m retro_backend.esim.cli",
        description="Run a frozen Restaurant E-Sim case against one model and grade it.",
    )
    sub = parser.add_subparsers(dest="command", required=True)

    run = sub.add_parser("run", help="run one model over all four worker roles")
    run.add_argument("--case", required=True, help="case id or path to its JSON file")
    run.add_argument("--model", required=True, help="provider spec, e.g. nim:openai/gpt-oss-20b")
    run.add_argument("--output-dir", required=True, help="directory to write the evidence bundle")
    run.add_argument(
        "--repeats",
        type=positive_int,
        default=1,
        help="runs of this case/model pair; the case seed stays fixed so repeats measure model variability",
    )
    # Default None, not the runner's values: an explicit default here would
    # silently override the runner and be recorded as a deliberate choice.
    run.add_argument("--max-attempts", type=positive_int, default=None)
    run.add_argument("--max-output-tokens", type=positive_int, default=None)
    run.add_argument("--max-turns", type=positive_int, default=None)
    run.add_argument(
        "--overwrite",
        action="store_true",
        help="replace an existing evidence bundle in --output-dir instead of refusing",
    )
    run.set_defaults(func=_run_command)

    regrade = sub.add_parser(
        "regrade",
        help="re-score a recorded trajectory under the current grader, with no model calls",
    )
    regrade.add_argument("--trajectory", required=True, help="path to a recorded trajectory.json")
    regrade.add_argument(
        "--output",
        default=None,
        help="write the new grade here; prints to stdout only when omitted",
    )
    regrade.set_defaults(func=_regrade_command)

    parity = sub.add_parser(
        "parity",
        help="check whether a foreign trace export can be graded by this engine",
    )
    parity.add_argument("--trace", required=True, help="path to a restaurant-arena-trace/v1 export")
    parity.add_argument("--output", default=None, help="write the parity report here as JSON")
    parity.set_defaults(func=_parity_command)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        return asyncio.run(args.func(args))
    except CaseError as exc:
        print(f"case error: {exc}", file=sys.stderr)
        return 2
    except BundleExistsError as exc:
        print(f"refusing to overwrite: {exc}", file=sys.stderr)
        return 3
    except TrajectoryLoadError as exc:
        print(f"unreadable trajectory: {exc}", file=sys.stderr)
        return 4
    except TraceLoadError as exc:
        print(f"unreadable trace: {exc}", file=sys.stderr)
        return 4
    except ValueError as exc:
        print(f"configuration error: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
