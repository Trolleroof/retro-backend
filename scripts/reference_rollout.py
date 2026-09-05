"""Drive a frozen case with a reference policy and grade it, offline (#75).

This is the determinism check the founder runs: reset, step, grade, twice, and
compare. No model, no network, no credentials.

**Why this is a script and not a CLI subcommand.** ``esim.policies`` is
infrastructure-only: nothing under ``src/`` may import it, so that a scripted
action can never be recorded or reported as a model decision (#75's acceptance
puts it as "no fallback actions are credited as model decisions"). Keeping the
policy driver outside ``src/`` preserves that guarantee mechanically rather
than by convention, at the cost of one extra documented command.

Usage:
    uv run python scripts/reference_rollout.py \\
        --case contracts/esim/cases/dinner-rush-001.json \\
        --policy greedy --repeats 2 --output-dir artifacts/ref-test/
"""

import argparse
import json
import sys
from collections.abc import Mapping
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from retro_backend.esim.case import ENV_VERSION, CaseError, load_case  # noqa: E402
from retro_backend.esim.contracts import ActionError, Role  # noqa: E402
from retro_backend.esim.grader import GRADER_VERSION, Grade, grade  # noqa: E402
from retro_backend.esim.policies import GreedyPolicy, IdlePolicy, Policy  # noqa: E402
from retro_backend.esim.rotation import ROLE_ORDER  # noqa: E402
from retro_backend.esim.simulator import RestaurantSim  # noqa: E402
from retro_backend.esim.state_hash import state_hash  # noqa: E402
from retro_backend.esim.trajectory import Trajectory, TrajectoryRecorder  # noqa: E402


def _policies(name: str, episode_id: str, actor_ids: Mapping[Role, str]) -> dict[Role, Policy]:
    """Build one policy per role, addressed to that case's real workers.

    Actor ids come from the loaded case rather than a constant here. Hardcoded
    ids happen to match ``dinner-rush-001``, but this command accepts any
    schema-valid case: against one whose workers are named differently, every
    action would be rejected as ``unknown_entity`` and the script would still
    run the horizon out and grade the resulting stalled rollout as though it
    were the reference policy.
    """
    factory = {"greedy": GreedyPolicy, "idle": IdlePolicy}[name]
    return {
        role: factory(episode_id=episode_id, actor_id=actor_ids[role], role=role)
        for role in ROLE_ORDER
    }


def rollout(case_id: str, policy_name: str) -> Trajectory:
    """Run one episode to completion under a reference policy."""
    case = load_case(case_id)
    sim = RestaurantSim(case.initial_state, event_schedule=case.event_schedule)
    state = sim.reset(case.seed)
    actor_ids: dict[Role, str] = {w.role: w.id for w in case.initial_state.workers}
    missing = [role for role in ROLE_ORDER if role not in actor_ids]
    if missing:
        raise ValueError(f"case {case_id!r} has no worker for role(s): {', '.join(missing)}")
    policies = _policies(policy_name, case.case_id, actor_ids)

    recorder = TrajectoryRecorder(
        case_id=case.case_id,
        seed=case.seed,
        env_version=ENV_VERSION,
        contract_version=case.contract_version,
        event_schedule=case.event_schedule,
    )
    recorder.record_state(state)

    while state.episode.phase == "active":
        for role in ROLE_ORDER:
            envelope = policies[role].decide(sim.observe(role))
            if envelope is None:
                continue
            result = sim.act(envelope)
            recorder.record_action(envelope, result)
            if not isinstance(result, ActionError):
                state = result
        state = sim.advance_time()
        recorder.record_state(state)

    return recorder.finish("episode_complete")


def _at_least_two(value: str) -> int:
    """Reject repeat counts a determinism check cannot draw a conclusion from.

    Below 1 left ``fingerprints`` empty and crashed on ``fingerprints[0]``.
    Exactly 1 was worse than a crash: it compared a run against itself and
    printed DETERMINISM OK, which is true of any run, deterministic or not.
    """
    parsed = int(value)
    if parsed < 2:
        raise argparse.ArgumentTypeError(
            f"a determinism check needs at least 2 runs to compare, got {parsed}"
        )
    return parsed


def _write_run(out_dir: Path, trajectory: Trajectory, graded: Grade, policy: str) -> None:
    out_dir.mkdir(parents=True, exist_ok=True)
    initial = trajectory.initial_state.model_dump(by_alias=True, mode="json")
    final = trajectory.final_state.model_dump(by_alias=True, mode="json")
    for name, payload in (
        ("manifest.json", {
            "envVersion": ENV_VERSION,
            "graderVersion": GRADER_VERSION,
            "caseId": trajectory.case_id,
            "seed": trajectory.seed,
            "driver": f"reference-policy:{policy}",
            "note": "offline reference policy, not a model run; never reported as a model result",
        }),
        ("initial-state.json", initial),
        ("final-state.json", final),
        ("trajectory.json", trajectory.model_dump(by_alias=True, mode="json")),
        ("grade.json", graded.model_dump(by_alias=True, mode="json")),
    ):
        (out_dir / name).write_text(json.dumps(payload, indent=1, sort_keys=True) + "\n")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Drive a frozen case with a reference policy and grade it, offline."
    )
    parser.add_argument("--case", required=True, help="case id or path to its JSON file")
    parser.add_argument("--policy", default="greedy", choices=("greedy", "idle"))
    parser.add_argument(
        "--repeats",
        type=_at_least_two,
        default=2,
        help="runs to compare (minimum 2, default 2)",
    )
    parser.add_argument("--output-dir", default=None, help="optional bundle destination")
    args = parser.parse_args(argv)

    case_id = Path(args.case).stem if args.case.endswith(".json") else args.case
    try:
        load_case(case_id)
    except CaseError as exc:
        print(f"case error: {exc}", file=sys.stderr)
        return 2

    fingerprints: list[tuple[str, str, str]] = []
    for repeat in range(1, args.repeats + 1):
        trajectory = rollout(case_id, args.policy)
        graded = grade(trajectory)
        initial_hash = state_hash(trajectory.initial_state.model_dump(by_alias=True))
        grade_blob = json.dumps(
            graded.model_dump(by_alias=True, mode="json"), sort_keys=True
        )
        fingerprints.append((initial_hash, trajectory.final_state_hash, grade_blob))
        if args.output_dir:
            root = Path(args.output_dir)
            out = root / f"repeat-{repeat:02d}" if args.repeats > 1 else root
            _write_run(out, trajectory, graded, args.policy)
        print(f"[{repeat}/{args.repeats}] initial={initial_hash[:23]} final={trajectory.final_state_hash[:23]}")

    first = fingerprints[0]
    identical = all(f == first for f in fingerprints)
    print()
    print(f"case={case_id} policy={args.policy} repeats={args.repeats}")
    print(f"initial hash identical : {all(f[0] == first[0] for f in fingerprints)}")
    print(f"final hash identical   : {all(f[1] == first[1] for f in fingerprints)}")
    print(f"grade identical        : {all(f[2] == first[2] for f in fingerprints)}")
    if not identical:
        print("\nDETERMINISM FAILED", file=sys.stderr)
        return 1
    print("\nDETERMINISM OK")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
