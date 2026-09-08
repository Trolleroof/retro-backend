"""Native Restaurant Arena controller contract and evidence precheck (#81).

The TypeScript game remains simulation authority. This module only validates
the completeness of its exported evidence before official judging.
"""

import json
from collections.abc import Mapping
from pathlib import Path
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, ValidationError, model_validator
from pydantic.alias_generators import to_camel

from retro_backend.esim.state_hash import state_hash

TASK_VERSION = "restaurant-controller-task/v1"
RUBRIC_VERSION = "restaurant-controller-rubric/v1"
EVIDENCE_VERSION = "restaurant-controller-evidence/v1"


class ControllerError(ValueError):
    pass


class _Model(BaseModel):
    model_config = ConfigDict(alias_generator=to_camel, populate_by_name=True, extra="forbid")


class Criterion(_Model):
    id: str
    source: Literal["mechanical", "judge"]
    metric: str | None = None
    operator: Literal["eq", "gte", "lte"] | None = None
    threshold: int | None = None

    @model_validator(mode="after")
    def complete_mechanical_rule(self) -> "Criterion":
        fields = (self.metric, self.operator, self.threshold)
        if self.source == "mechanical" and any(item is None for item in fields):
            raise ValueError("mechanical criterion requires metric, operator, and threshold")
        if self.source == "judge" and any(item is not None for item in fields):
            raise ValueError("judge criterion cannot claim a mechanical threshold")
        return self


class Task(_Model):
    task_version: Literal["restaurant-controller-task/v1"]
    rubric_version: Literal["restaurant-controller-rubric/v1"]
    status: Literal["development", "frozen"]
    calibrated_on: tuple[str, ...] = Field(min_length=1)
    environment_version: str
    manager_adapter_version: Literal["restaurant-arena-manager/v1"]
    scenario: str
    seeds: tuple[int, ...] = Field(min_length=1)
    budget: Mapping[str, object]
    criteria: tuple[Criterion, ...] = Field(min_length=1)
    scoring: Mapping[str, object]
    notes: tuple[str, ...] = ()


class NativeEvidence(_Model):
    evidence_version: Literal["restaurant-controller-evidence/v1"] = EVIDENCE_VERSION
    task_version: Literal["restaurant-controller-task/v1"] = TASK_VERSION
    rubric_version: Literal["restaurant-controller-rubric/v1"] = RUBRIC_VERSION
    run_id: str
    scenario: str
    seed: int
    controller: Mapping[str, object]
    crew: Mapping[str, object]
    environment: Mapping[str, object]
    budget: Mapping[str, object]
    initial_checkpoint: Mapping[str, object] = Field(min_length=2)
    final_checkpoint: Mapping[str, object] = Field(min_length=2)
    events: tuple[Mapping[str, object], ...] = Field(min_length=1)
    metrics: Mapping[str, int]
    handover: str = Field(min_length=1)
    termination_reason: str = Field(min_length=1)
    provider_errors: tuple[Mapping[str, object], ...] = ()
    fallbacks: tuple[Mapping[str, object], ...] = ()


def load_task(path: Path) -> Task:
    try:
        return Task.model_validate_json(path.read_text())
    except (OSError, ValidationError) as exc:
        raise ControllerError(f"invalid task {path}: {exc}") from exc


def load_evidence(path: Path) -> NativeEvidence:
    try:
        return NativeEvidence.model_validate_json(path.read_text())
    except (OSError, ValidationError) as exc:
        raise ControllerError(f"invalid evidence {path}: {exc}") from exc


def mechanical_precheck(task: Task, evidence: NativeEvidence) -> dict[str, Any]:
    """Check packet completeness; never return an official or composite grade."""
    if task.status != "frozen":
        return {"status": "ungradeable", "reason": "task_not_frozen", "finalGrade": False}
    if evidence.scenario != task.scenario or evidence.seed not in task.seeds:
        raise ControllerError("evidence scenario/seed is outside the frozen task")
    if evidence.provider_errors:
        return {"status": "invalid", "reason": "provider_error", "finalGrade": False}

    for name, checkpoint in (
        ("initial", evidence.initial_checkpoint),
        ("final", evidence.final_checkpoint),
    ):
        digest, state = checkpoint.get("hash"), checkpoint.get("state")
        if (
            not isinstance(digest, str)
            or not isinstance(state, Mapping)
            or digest != state_hash(state)
        ):
            return {
                "status": "ungradeable",
                "reason": f"invalid {name} checkpoint",
                "finalGrade": False,
            }
    for sequence, event in enumerate(evidence.events):
        if event.get("sequence") != sequence or not all(
            event.get(key) for key in ("actor", "provenance")
        ):
            return {
                "status": "ungradeable",
                "reason": "events require contiguous sequence, actor, and provenance",
                "finalGrade": False,
            }

    results: list[dict[str, object]] = []
    for criterion in (item for item in task.criteria if item.source == "mechanical"):
        assert (
            criterion.metric is not None
            and criterion.operator is not None
            and criterion.threshold is not None
        )
        if criterion.metric not in evidence.metrics:
            return {
                "status": "ungradeable",
                "reason": f"missing metric: {criterion.metric}",
                "finalGrade": False,
            }
        actual = evidence.metrics[criterion.metric]
        passed = (
            actual == criterion.threshold
            if criterion.operator == "eq"
            else actual >= criterion.threshold
            if criterion.operator == "gte"
            else actual <= criterion.threshold
        )
        results.append(
            {
                "id": criterion.id,
                "passed": passed,
                "metric": criterion.metric,
                "actual": actual,
                "threshold": criterion.threshold,
            }
        )
    return {
        "status": "ready_for_archipelago",
        "mechanicalChecks": results,
        "rawMetrics": dict(evidence.metrics),
        "judgeRequired": [item.id for item in task.criteria if item.source == "judge"],
        "finalGrade": False,
    }


def public_task_prompt(task: Task) -> dict[str, object]:
    """Objectives shown identically to the human UI and controller prompt."""
    return {
        "taskVersion": task.task_version,
        "scenario": task.scenario,
        "budget": dict(task.budget),
        "criteria": [item.model_dump(by_alias=True, mode="json") for item in task.criteria],
        "scoring": dict(task.scoring),
    }


def write_precheck(task_path: Path, evidence_path: Path, output: Path) -> dict[str, Any]:
    result = mechanical_precheck(load_task(task_path), load_evidence(evidence_path))
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(result, indent=2, sort_keys=True) + "\n")
    return result
