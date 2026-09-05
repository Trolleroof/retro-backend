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
from retro_backend.esim.runner import RunnerConfig, RunResult, run_model_case
from retro_backend.esim.state_hash import state_hash
from retro_backend.esim.trajectory import Trajectory

# Bumped when the bundle's file set or field meanings change, so a report can
# tell a v1 bundle from a later one without guessing from its contents.
BUNDLE_VERSION = "esim-run-bundle/v1"


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
            "maxTurns": config.max_turns,
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


async def _run_once(config: RunnerConfig, out_dir: Path, *, repeat: int, repeats: int) -> Grade:
    started_at = datetime.now(UTC).isoformat()
    case = load_case(config.case_id)
    out_dir.mkdir(parents=True, exist_ok=True)
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
            repeat=repeat,
            repeats=repeats,
            started_at=started_at,
        ),
        result=result,
        graded=graded,
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
    config = RunnerConfig(
        case_id=case_id,
        model=args.model,
        max_attempts=args.max_attempts,
        max_output_tokens=args.max_output_tokens,
        max_turns=args.max_turns,
    )
    root = Path(args.output_dir)
    print(f"case={case_id} model={args.model} repeats={args.repeats}")
    for repeat in range(1, args.repeats + 1):
        out_dir = root / f"repeat-{repeat:02d}" if args.repeats > 1 else root
        print(f"[{repeat}/{args.repeats}] running...")
        graded = await _run_once(config, out_dir, repeat=repeat, repeats=args.repeats)
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
        type=int,
        default=1,
        help="runs of this case/model pair; the case seed stays fixed so repeats measure model variability",
    )
    run.add_argument("--max-attempts", type=int, default=3)
    run.add_argument("--max-output-tokens", type=int, default=256)
    run.add_argument("--max-turns", type=int, default=None)
    run.set_defaults(func=_run_command)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        return asyncio.run(args.func(args))
    except CaseError as exc:
        print(f"case error: {exc}", file=sys.stderr)
        return 2
    except ValueError as exc:
        print(f"configuration error: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
