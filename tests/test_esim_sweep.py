"""Focused orchestration checks; no provider calls are made here."""

# Callback doubles are deliberately structural and private helpers are tested
# directly here; keep the production typecheck strict without noisy fixture warnings.
# pyright: reportPrivateUsage=false, reportUnknownParameterType=false, reportMissingParameterType=false, reportUnusedParameter=false, reportUnknownLambdaType=false

import asyncio
import json
from dataclasses import replace
from pathlib import Path

import pytest

from retro_backend.esim import sweep as sweep_module
from retro_backend.esim.sweep import SweepConfig, _component, main, run_sweep


def _inputs(tmp_path: Path, *, models: list[str] | None = None, version: str = "bench/v1") -> SweepConfig:
    registry = tmp_path / "registry.json"
    registry.write_text(json.dumps({
        "status": "frozen",
        "cases": [{
            "caseId": "dinner-rush-001",
            "benchmarkVersion": version,
            "seed": 218220,
            "horizon": 24,
            "startingState": {},
            "eventSchedule": [],
            "thresholds": {},
            "expectedIncident": {},
        }],
    }))
    roster = tmp_path / "models.json"
    roster.write_text(json.dumps(models or ["nim:model-a", "nim:model-b"]))
    return SweepConfig(version, registry, roster, tmp_path / "runs", max_concurrency=2)


def _write_bundle(out_dir: Path, *, model: str, version: str, valid: bool, attempt: int) -> None:
    out_dir.mkdir(parents=True, exist_ok=True)
    (out_dir / "manifest.json").write_text(json.dumps({"benchmarkVersion": version, "model": model}))
    for name in ("attempts.jsonl", "trajectory.json"):
        (out_dir / name).write_text("{}\n")
    (out_dir / "grade.json").write_text(json.dumps({
        "metrics": {"Service": 0.8, "Resources": 0.7, "Recovery": 0.9, "Safety": 1.0, "Composite": 0.85},
        "allRequirementsMet": True,
    }))
    (out_dir / "summary.json").write_text(json.dumps({
        "benchmarkVersion": version,
        "model": model,
        "runValidity": "valid" if valid else "provider_invalid",
        "termination": "episode_complete" if valid else "provider_invalid",
        "servedModels": [model.removeprefix("nim:")],
        "turns": attempt,
    }))


@pytest.mark.asyncio
async def test_sweep_never_exceeds_configured_model_concurrency(tmp_path: Path) -> None:
    config = _inputs(tmp_path)
    active = 0
    peak = 0

    async def fake_runner(model, case_id, out_dir, attempt, sweep_config):
        nonlocal active, peak
        active += 1
        peak = max(peak, active)
        await asyncio.sleep(0)
        _write_bundle(out_dir, model=model, version=sweep_config.benchmark_version, valid=True, attempt=attempt)
        active -= 1

    await run_sweep(config, episode_runner=fake_runner)
    assert peak <= config.max_concurrency


@pytest.mark.asyncio
async def test_dry_run_validates_draft_registry_without_calling_provider(tmp_path: Path) -> None:
    config = _inputs(tmp_path, models=["nim:model-a"])
    dry = replace(config, dry_run=True)

    async def should_not_run(*args):
        raise AssertionError("dry-run called an episode runner")

    report = await run_sweep(dry, episode_runner=should_not_run)
    assert report["dry_run"] is True
    assert report["planned_episodes"] == 1


@pytest.mark.asyncio
async def test_completed_episode_is_skipped_on_resume(tmp_path: Path) -> None:
    config = _inputs(tmp_path, models=["nim:model-a"])
    calls = 0

    async def fake_runner(model, case_id, out_dir, attempt, sweep_config):
        nonlocal calls
        calls += 1
        _write_bundle(out_dir, model=model, version=sweep_config.benchmark_version, valid=True, attempt=attempt)

    await run_sweep(config, episode_runner=fake_runner)
    await run_sweep(config, episode_runner=fake_runner)
    assert calls == 1


@pytest.mark.asyncio
async def test_provider_invalid_episode_reruns_when_requested(tmp_path: Path) -> None:
    config = _inputs(tmp_path, models=["nim:model-a"])
    config = replace(config, rerun_provider_invalid=True)
    attempts: list[int] = []

    async def fake_runner(model, case_id, out_dir, attempt, sweep_config):
        attempts.append(attempt)
        _write_bundle(out_dir, model=model, version=sweep_config.benchmark_version, valid=attempt == 2, attempt=attempt)

    # The first call creates the invalid attempt; a second sweep is the explicit rerun.
    first = replace(config, rerun_provider_invalid=False)
    await run_sweep(first, episode_runner=fake_runner)
    await run_sweep(config, episode_runner=fake_runner)
    assert attempts == [1, 2]


@pytest.mark.asyncio
async def test_repeated_provider_failures_stop_a_model_and_split_the_report(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    config = _inputs(tmp_path, models=["nim:model-a"])
    monkeypatch.setattr(
        sweep_module,
        "validate_sweep_inputs",
        lambda _: (("nim:model-a",), ("dinner-rush-001", "dinner-rush-002", "dinner-rush-003")),
    )
    calls: list[str] = []

    async def fake_runner(model, case_id, out_dir, attempt, sweep_config):
        calls.append(case_id)
        _write_bundle(out_dir, model=model, version=sweep_config.benchmark_version, valid=False, attempt=attempt)

    report = await run_sweep(config, episode_runner=fake_runner)
    model = report["models"][0]
    assert calls == ["dinner-rush-001", "dinner-rush-002"]
    assert model["task"]["skipped_after_provider_failures"] == 1
    assert model["provider"]["stopped_after_repeated_provider_failures"] is True
    assert model["trajectory"]["valid_trajectory_episodes"] == 0


@pytest.mark.asyncio
async def test_fewer_than_eight_valid_episodes_is_not_publishable(tmp_path: Path) -> None:
    config = _inputs(tmp_path, models=["nim:model-a"])

    async def fake_runner(model, case_id, out_dir, attempt, sweep_config):
        _write_bundle(out_dir, model=model, version=sweep_config.benchmark_version, valid=False, attempt=attempt)

    report = await run_sweep(config, episode_runner=fake_runner)
    assert report["models"][0]["valid_episodes"] == 0
    assert report["models"][0]["Service"] == "N/A"


@pytest.mark.asyncio
async def test_turn_limited_episode_is_not_valid_for_aggregation(tmp_path: Path) -> None:
    config = _inputs(tmp_path, models=["nim:model-a"])

    async def fake_runner(model, case_id, out_dir, attempt, sweep_config):
        _write_bundle(out_dir, model=model, version=sweep_config.benchmark_version, valid=True, attempt=attempt)
        summary_path = out_dir / "summary.json"
        summary = json.loads(summary_path.read_text())
        summary["termination"] = "turn_limit"
        summary_path.write_text(json.dumps(summary))

    report = await run_sweep(config, episode_runner=fake_runner)
    assert report["models"][0]["valid_episodes"] == 0
    assert report["models"][0]["Service"] == "N/A"


def test_v2_report_excludes_unverified_replays(tmp_path: Path) -> None:
    out_dir = tmp_path / "attempt"
    _write_bundle(out_dir, model="openrouter:model-a", version="restaurant-fixed-workers/v2", valid=True, attempt=1)
    grade_path = out_dir / "grade.json"
    grade = json.loads(grade_path.read_text())
    grade.update({"runValidity": "valid", "exclusionReason": None, "replayVerified": False})
    grade_path.write_text(json.dumps(grade))
    entry = {"summary": json.loads((out_dir / "summary.json").read_text()), "grade": grade}
    report = sweep_module._model_report(
        "openrouter:model-a", [entry], "restaurant-fixed-workers/v2", artifact_root=tmp_path,
        stopped_after_provider_failures=False, planned_episodes=1,
    )
    assert report["valid_episodes"] == 0


def test_sweep_component_rejects_dot_segments() -> None:
    assert _component(".") == "__"
    assert _component("..") == "__"


def test_sweep_cli_reports_missing_frozen_case_without_traceback(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    config = _inputs(tmp_path, models=["nim:model-a"])
    registry = json.loads(config.registry_path.read_text())
    registry["cases"][0]["caseId"] = "not-a-real-case"
    config.registry_path.write_text(json.dumps(registry))

    result = main([
        "--benchmark-version", config.benchmark_version,
        "--case-registry", str(config.registry_path),
        "--model-roster", str(config.roster_path),
        "--output-dir", str(config.output_dir),
    ])

    assert result == 2
    assert "sweep configuration error:" in capsys.readouterr().out


@pytest.mark.asyncio
async def test_scores_use_only_valid_episodes_and_models_have_separate_dirs(tmp_path: Path) -> None:
    config = _inputs(tmp_path)

    async def fake_runner(model, case_id, out_dir, attempt, sweep_config):
        _write_bundle(out_dir, model=model, version=sweep_config.benchmark_version, valid=model.endswith("a"), attempt=attempt)

    report = await run_sweep(config, episode_runner=fake_runner)
    assert report["models"][0]["valid_episodes"] == 1
    assert report["models"][0]["provider_invalid_episodes"] == 0
    assert (config.output_dir / "nim__model-a").is_dir()
    assert (config.output_dir / "nim__model-b").is_dir()


@pytest.mark.asyncio
async def test_changed_benchmark_version_does_not_reuse_old_artifacts(tmp_path: Path) -> None:
    first = _inputs(tmp_path, models=["nim:model-a"], version="bench/v1")
    calls: list[Path] = []

    async def fake_runner(model, case_id, out_dir, attempt, sweep_config):
        calls.append(out_dir)
        _write_bundle(out_dir, model=model, version=sweep_config.benchmark_version, valid=True, attempt=attempt)

    await run_sweep(first, episode_runner=fake_runner)
    second = _inputs(tmp_path, models=["nim:model-a"], version="bench/v2")
    await run_sweep(second, episode_runner=fake_runner)
    assert len(calls) == 2
    assert calls[1].name == "attempt-02"


@pytest.mark.asyncio
async def test_partial_sweep_resumes_without_overwriting_attempts(tmp_path: Path) -> None:
    config = _inputs(tmp_path, models=["nim:model-a"])
    first = True

    async def flaky_runner(model, case_id, out_dir, attempt, sweep_config):
        nonlocal first
        _write_bundle(out_dir, model=model, version=sweep_config.benchmark_version, valid=False, attempt=attempt)
        if first:
            first = False
            raise RuntimeError("simulated interruption")

    with pytest.raises(RuntimeError):
        await run_sweep(config, episode_runner=flaky_runner)
    await run_sweep(replace(config, rerun_provider_invalid=True), episode_runner=lambda *args: _resume_runner(*args))
    assert (config.output_dir / "nim__model-a" / "dinner-rush-001" / "attempt-02").is_dir()


async def _resume_runner(model, case_id, out_dir, attempt, sweep_config):
    _write_bundle(out_dir, model=model, version=sweep_config.benchmark_version, valid=True, attempt=attempt)
