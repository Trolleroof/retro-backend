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

from retro_backend.esim.case import ENV_VERSION, CaseError, load_case
from retro_backend.esim.grader import GRADER_VERSION, Grade, grade
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
    benchmark_version: str | None = None,
    episode_id: str | None = None,
    attempt_number: int = 1,
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
        "benchmarkVersion": benchmark_version,
        "episodeId": episode_id,
        "attemptNumber": attempt_number,
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
            "requestTimeoutSeconds": config.request_timeout,
            "retryBudget": config.max_attempts - 1,
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
    summary_fields: dict[str, object] | None = None,
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
            **(summary_fields or {}),
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
    config: RunnerConfig,
    out_dir: Path,
    *,
    repeat: int,
    repeats: int,
    overwrite: bool,
    benchmark_version: str | None = None,
    episode_id: str | None = None,
    attempt_number: int = 1,
) -> Grade:
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
            benchmark_version=benchmark_version,
            episode_id=episode_id,
            attempt_number=attempt_number,
        ),
        result=result,
        graded=graded,
        summary_fields={
            "benchmarkVersion": benchmark_version,
            "episodeId": episode_id,
            "attemptNumber": attempt_number,
        },
    )
    return graded


def _report(graded: Grade, summary: dict[str, Any], out_dir: Path) -> None:
    o, b = graded.outcome, graded.behavior
    print(f"  -> {out_dir}")
    print(f"     termination={graded.termination} ticks={graded.ticks} safetyFailure={graded.safety_failure}")
    print(f"     safety={o.safety} completion={o.order_completion} wait={o.wait_time} waste={o.food_waste} satisfaction={o.customer_satisfaction}")
    print(f"     coordination={b.coordination} scopeControl={b.scope_control} correctionUptake={b.correction_uptake}")
    if summary.get("servedModelMismatch"):
        print(f"     !! served model mismatch: {summary.get('servedModels')}")


async def _run_command(args: argparse.Namespace) -> int:
    case_id = resolve_case_id(args.case)
    # Only budgets the caller actually named are passed through. Repeating the
    # runner's defaults here once shadowed them: the CLI kept sending its own
    # stale 256-token budget after the runner's had been raised, and every run
    # went out under a budget the manifest then faithfully recorded as chosen.
    overrides: dict[str, Any] = {
        name: value
        for name, value in (
            ("max_attempts", args.max_attempts),
            ("max_output_tokens", args.max_output_tokens),
            ("max_turns", args.max_turns),
            ("request_timeout", args.request_timeout),
        )
        if value is not None
    }
    config = RunnerConfig(case_id=case_id, model=args.model, **overrides)
    root = Path(args.output_dir)
    print(f"case={case_id} model={args.model} repeats={args.repeats}")
    for repeat in range(1, args.repeats + 1):
        out_dir = root / f"repeat-{repeat:02d}" if args.repeats > 1 else root
        print(f"[{repeat}/{args.repeats}] running...")
        graded = await _run_once(
            config, out_dir, repeat=repeat, repeats=args.repeats, overwrite=args.overwrite
        )
        summary = json.loads((out_dir / "summary.json").read_text())
        _report(graded, summary, out_dir)
    return 0


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
    run.add_argument("--request-timeout", type=float, default=None)
    run.add_argument(
        "--overwrite",
        action="store_true",
        help="replace an existing evidence bundle in --output-dir instead of refusing",
    )
    run.set_defaults(func=_run_command)
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
    except ValueError as exc:
        print(f"configuration error: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
