"""Prepare native controller evidence for the pinned Archipelago grader (#80)."""

import json
import zipfile
from pathlib import Path

from retro_backend.esim.controller import NativeEvidence, Task, mechanical_precheck

ARCHIPELAGO_REPOSITORY = "https://github.com/Mercor-Intelligence/archipelago.git"
ARCHIPELAGO_REVISION = "e0720e01db09a904810412e0209d0079045591ec"


def _json(path: Path, value: object) -> None:
    path.write_text(json.dumps(value, indent=2, sort_keys=True) + "\n")


def _snapshot(path: Path, files: dict[str, str]) -> None:
    with zipfile.ZipFile(path, "w", zipfile.ZIP_DEFLATED) as archive:
        for name, content in sorted(files.items()):
            archive.writestr(name, content)


def prepare_packet(task: Task, evidence: NativeEvidence, output: Path, *, judge_model: str) -> None:
    """Write official-grader inputs; this does not claim the judge was run."""
    mechanical = mechanical_precheck(task, evidence)
    if mechanical["status"] != "ready_for_archipelago":
        raise ValueError(f"evidence is not ready for Archipelago: {mechanical['reason']}")
    output.mkdir(parents=True, exist_ok=False)
    task_json = task.model_dump_json(by_alias=True, indent=2)
    evidence_json = evidence.model_dump_json(by_alias=True, indent=2)
    _snapshot(output / "initial_snapshot.zip", {"task.json": task_json})
    _snapshot(
        output / "final_snapshot.zip",
        {
            "task.json": task_json,
            "evidence.json": evidence_json,
            "handover.txt": evidence.handover,
            "mechanical-grade.json": json.dumps(mechanical, indent=2, sort_keys=True),
        },
    )
    _json(
        output / "trajectory.json",
        {
            "messages": [{"role": "assistant", "content": evidence.handover}],
            "output": {"runId": evidence.run_id},
            "status": "completed",
            "time_elapsed": 0,
        },
    )
    _json(
        output / "grading_settings.json",
        {"llm_judge_model": judge_model, "llm_judge_extra_args": None},
    )
    _json(
        output / "eval_configs.json",
        [
            {
                "eval_config_id": "controller-output",
                "eval_config_name": "Controller evidence output judge",
                "eval_defn_id": "output_llm",
                "eval_config_values": {},
            }
        ],
    )
    _json(
        output / "verifiers.json",
        [
            {
                "verifier_id": f"controller-{criterion.id}",
                "verifier_version": 1,
                "world_id": None,
                "task_id": task.task_version,
                "eval_config_id": "controller-output",
                "verifier_values": {
                    "criteria": f"Using immutable evidence.json and the separate handover.txt, determine whether required criterion '{criterion.id}' is satisfied. Cite concrete evidence and do not infer missing facts.",
                    "is_primary_objective": True,
                    "artifacts_to_reference": [
                        "evidence.json",
                        "handover.txt",
                        "mechanical-grade.json",
                    ],
                },
                "verifier_index": index,
                "verifier_dependencies": None,
            }
            for index, criterion in enumerate(task.criteria)
        ],
    )
    _json(
        output / "scoring_config.json",
        {
            "scoring_config_id": "controller-equal",
            "scoring_config_name": "Equal required criteria",
            "scoring_defn_id": "task_score_unweighted_and_universal_penalty",
            "scoring_config_values": {
                "task_primary_objective_scaling_factor": 1.0,
                "task_non_primary_objective_scaling_factor": 1.0,
                "task_negative_scaling_factor": 1.0,
                "universal_penalty_cap": 0.0,
                "final_score_ceiling": 1.0,
                "final_score_floor": 0.0,
            },
        },
    )
    _json(
        output / "archipelago-manifest.json",
        {
            "repository": ARCHIPELAGO_REPOSITORY,
            "revision": ARCHIPELAGO_REVISION,
            "judgeModel": judge_model,
            "status": "prepared_not_graded",
            "command": "uv run python -m runner.main --grading-run-id RUN_ID --trajectory-id TRAJECTORY_ID --initial-snapshot PACKET/initial_snapshot.zip --final-snapshot PACKET/final_snapshot.zip --trajectory PACKET/trajectory.json --grading-settings PACKET/grading_settings.json --verifiers PACKET/verifiers.json --eval-configs PACKET/eval_configs.json --scoring-config PACKET/scoring_config.json --output PACKET/grades.json",
        },
    )
