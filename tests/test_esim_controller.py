import json
import zipfile
from pathlib import Path

import pytest

from retro_backend.esim import archipelago as archipelago_module
from retro_backend.esim.archipelago import ARCHIPELAGO_REVISION, prepare_packet
from retro_backend.esim.controller import (
    EVIDENCE_VERSION,
    load_task,
    mechanical_precheck,
    public_task_prompt,
)
from retro_backend.esim.state_hash import state_hash

TASK = Path("contracts/esim/controller-task-v1.json")


def evidence(**overrides: object):
    task = load_task(TASK)
    payload = {
        "evidenceVersion": EVIDENCE_VERSION,
        "taskVersion": task.task_version,
        "rubricVersion": task.rubric_version,
        "runId": "smoke-1",
        "scenario": task.scenario,
        "seed": task.seeds[0],
        "controller": {"provider": "stub", "model": "controller"},
        "crew": {"provider": "stub", "model": "worker", "fixed": True},
        "environment": {"revision": "abc", "managerAdapterVersion": task.manager_adapter_version},
        "budget": task.budget,
        "initialCheckpoint": {"hash": state_hash({"tick": 0}), "state": {"tick": 0}},
        "finalCheckpoint": {"hash": state_hash({"tick": 2}), "state": {"tick": 2}},
        "events": [{"sequence": 0, "actor": "system", "provenance": "native"}],
        "metrics": {"unsafeServes": 0, "servedOrders": 2, "wasteUnits": 1, "recoveredIncidents": 1},
        "handover": "Two orders served; oven recovered.",
        "terminationReason": "complete",
        **overrides,
    }
    from retro_backend.esim.controller import NativeEvidence

    return task, NativeEvidence.model_validate(payload)


def test_frozen_task_only_returns_mechanical_precheck() -> None:
    task, run = evidence()
    task = task.model_copy(update={"status": "frozen"})
    result = mechanical_precheck(task, run)
    assert result["status"] == "ready_for_archipelago"
    assert result["finalGrade"] is False
    assert result["judgeRequired"] == ["delegation", "handover"]


def test_provider_failure_is_invalid_not_model_failure() -> None:
    task, run = evidence(providerErrors=[{"actor": "worker", "error": "timeout"}])
    task = task.model_copy(update={"status": "frozen"})
    assert mechanical_precheck(task, run) == {
        "status": "invalid",
        "reason": "provider_error",
        "finalGrade": False,
    }


def test_missing_metric_is_ungradeable() -> None:
    task, run = evidence(metrics={"unsafeServes": 0})
    task = task.model_copy(update={"status": "frozen"})
    assert mechanical_precheck(task, run)["status"] == "ungradeable"


def test_self_reported_checkpoint_hash_is_not_trusted() -> None:
    task, run = evidence(initialCheckpoint={"hash": "sha256:" + "0" * 64, "state": {"tick": 0}})
    task = task.model_copy(update={"status": "frozen"})
    assert mechanical_precheck(task, run)["reason"] == "invalid initial checkpoint"


def test_development_thresholds_cannot_be_reported_as_a_grade() -> None:
    task, run = evidence()
    assert mechanical_precheck(task, run)["reason"] == "task_not_frozen"
    assert public_task_prompt(task)["criteria"] == [
        item.model_dump(by_alias=True, mode="json") for item in task.criteria
    ]


def test_archipelago_packet_keeps_system_evidence_and_handover_separate(tmp_path: Path) -> None:
    task, run = evidence()
    task = task.model_copy(update={"status": "frozen"})
    out = tmp_path / "packet"
    prepare_packet(task, run, out, judge_model="openai/gpt-5")
    manifest = json.loads((out / "archipelago-manifest.json").read_text())
    assert manifest["revision"] == ARCHIPELAGO_REVISION
    assert manifest["status"] == "prepared_not_graded"
    with zipfile.ZipFile(out / "final_snapshot.zip") as archive:
        assert set(archive.namelist()) == {
            "task.json",
            "evidence.json",
            "handover.txt",
            "mechanical-grade.json",
        }
        assert json.loads(archive.read("evidence.json"))["events"][0]["actor"] == "system"
        assert archive.read("handover.txt").decode() == run.handover
    verifiers = json.loads((out / "verifiers.json").read_text())
    assert len(verifiers) == len(task.criteria)
    assert {row["verifier_values"]["artifacts_to_reference"][0] for row in verifiers} == {
        "evidence.json"
    }


def test_invalid_packet_attempt_leaves_output_available_for_retry(tmp_path: Path) -> None:
    task, run = evidence()
    output = tmp_path / "packet"
    with pytest.raises(ValueError, match="not ready for Archipelago"):
        prepare_packet(task, run, output, judge_model="openai/gpt-5")
    assert not output.exists()

    frozen = task.model_copy(update={"status": "frozen"})
    prepare_packet(frozen, run, output, judge_model="openai/gpt-5")
    assert (output / "archipelago-manifest.json").is_file()


def test_failed_packet_write_leaves_output_available_for_retry(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    task, run = evidence()
    task = task.model_copy(update={"status": "frozen"})
    output = tmp_path / "packet"
    original_json = archipelago_module._json

    def fail_once(path: Path, value: object) -> None:
        monkeypatch.setattr(archipelago_module, "_json", original_json)
        raise OSError("simulated write failure")

    monkeypatch.setattr(archipelago_module, "_json", fail_once)
    with pytest.raises(OSError, match="simulated write failure"):
        prepare_packet(task, run, output, judge_model="openai/gpt-5")
    assert not output.exists()

    prepare_packet(task, run, output, judge_model="openai/gpt-5")
    assert (output / "archipelago-manifest.json").is_file()
