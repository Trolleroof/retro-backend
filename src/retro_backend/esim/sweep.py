"""Bounded orchestration around the existing single-case E-Sim runner."""

import argparse
import asyncio
import json
import re
from collections.abc import Awaitable, Callable, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from retro_backend.esim.case import (
    FIXED_WORKER_BENCHMARK_VERSION_V2,
    CaseError,
    load_case,
    load_fixed_worker_case_v2,
)
from retro_backend.esim.cli import _run_once  # pyright: ignore[reportPrivateUsage]
from retro_backend.esim.fixed_workers_v2_runner import FixedWorkersV2Config, run_fixed_workers_v2
from retro_backend.esim.runner import RunnerConfig

REQUIRED_ARTIFACTS = (
    "manifest.json",
    "attempts.jsonl",
    "trajectory.json",
    "grade.json",
    "summary.json",
)


@dataclass(frozen=True, slots=True)
class SweepConfig:
    benchmark_version: str
    registry_path: Path
    roster_path: Path
    output_dir: Path
    max_concurrency: int = 2
    request_timeout: float = 30.0
    retry_budget: int = 2
    max_output_tokens: int = 2500
    max_turns: int | None = None
    rerun_provider_invalid: bool = False
    dry_run: bool = False
    engine_repo: Path | None = None
    nim_api_keys_file: Path | None = None
    openrouter_api_key_file: Path | None = None
    models: tuple[str, ...] = ()
    case_ids: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        if not self.benchmark_version.strip():
            raise ValueError("benchmark_version must not be empty")
        if self.max_concurrency < 1 or self.retry_budget < 0 or self.max_output_tokens < 1:
            raise ValueError("max_concurrency must be positive; retry_budget must not be negative")
        if self.request_timeout <= 0:
            raise ValueError("request_timeout must be positive")
        if self.max_turns is not None and self.max_turns < 1:
            raise ValueError("max_turns must be positive when set")
        if (
            self.benchmark_version == FIXED_WORKER_BENCHMARK_VERSION_V2
            and not self.dry_run
            and (
                self.engine_repo is None
                or not (self.engine_repo / "src/games/restaurant-arena/restaurant-game.ts").is_file()
            )
        ):
            raise ValueError("v2 requires --engine-repo pointing to a merged Restaurant Arena checkout")


EpisodeRunner = Callable[[str, str, Path, int, SweepConfig], Awaitable[None]]


def _read_json(path: Path) -> object:
    try:
        return json.loads(path.read_text())
    except (OSError, json.JSONDecodeError) as exc:
        raise ValueError(f"cannot read JSON {path}: {exc}") from exc


def _model_roster(path: Path) -> tuple[str, ...]:
    payload = _read_json(path)
    values: object = payload.get("models") if isinstance(payload, dict) else payload
    if not isinstance(values, list):
        raise ValueError("model roster must be a JSON array or an object with a models array")
    models: list[str] = []
    for value in values:
        model = value.get("model") if isinstance(value, dict) else value
        if not isinstance(model, str) or not model.strip():
            raise ValueError("every roster entry must be a non-empty model string")
        models.append(model.strip())
    if not models or len(set(models)) != len(models):
        raise ValueError("model roster must contain unique models")
    return tuple(models)


def _case_registry(path: Path, benchmark_version: str, *, dry_run: bool) -> tuple[str, ...]:
    payload = _read_json(path)
    if not isinstance(payload, dict):
        raise ValueError("case registry must be an object")
    case_values = payload.get("heldOutCases") if benchmark_version == FIXED_WORKER_BENCHMARK_VERSION_V2 else payload.get("cases")
    if not isinstance(case_values, list):
        raise ValueError("case registry does not contain the expected cases")
    if not dry_run and payload.get("status") != "frozen":
        raise ValueError("case registry is not frozen; pass --dry-run until Step 3 and Step 4 pass")
    case_ids: list[str] = []
    for case in case_values:
        if benchmark_version == FIXED_WORKER_BENCHMARK_VERSION_V2:
            if not isinstance(case, str):
                raise ValueError("every v2 registry case must be a caseId string")
            case_ids.append(case)
            if not dry_run:
                loaded = load_fixed_worker_case_v2(case)
                if loaded.benchmark_version != benchmark_version:
                    raise ValueError(f"case {case!r} does not match benchmark version {benchmark_version!r}")
            continue
        if not isinstance(case, dict) or not isinstance(case.get("caseId"), str):
            raise ValueError("every registry case needs a caseId")
        case_id = case["caseId"]
        required = ("benchmarkVersion", "seed", "horizon", "startingState", "eventSchedule", "thresholds", "expectedIncident")
        missing = [field for field in required if field not in case]
        if missing:
            raise ValueError(f"case {case_id!r} is missing registry fields: {', '.join(missing)}")
        version = case.get("benchmarkVersion")
        if version != benchmark_version:
            raise ValueError(f"case {case_id!r} does not match benchmark version {benchmark_version!r}")
        case_ids.append(case_id)
        if not dry_run:
            load_case(case_id)
    if not case_ids or len(set(case_ids)) != len(case_ids):
        raise ValueError("case registry must contain unique cases")
    return tuple(case_ids)


def validate_sweep_inputs(config: SweepConfig) -> tuple[tuple[str, ...], tuple[str, ...]]:
    models = _model_roster(config.roster_path)
    cases = _case_registry(config.registry_path, config.benchmark_version, dry_run=config.dry_run)
    if config.models:
        unknown = set(config.models) - set(models)
        if unknown:
            raise ValueError(f"requested models are not in roster: {', '.join(sorted(unknown))}")
        models = config.models
    if config.case_ids:
        unknown = set(config.case_ids) - set(cases)
        if unknown:
            raise ValueError(f"requested cases are not in registry: {', '.join(sorted(unknown))}")
        cases = config.case_ids
    return models, cases


def _component(value: str) -> str:
    component = re.sub(r"[^A-Za-z0-9._-]+", "__", value)
    return "__" if component in {".", ".."} else component


def _attempt_dirs(case_dir: Path) -> list[Path]:
    paths = [case_dir] if case_dir.is_dir() else []
    paths.extend(sorted(case_dir.glob("attempt-*")) if case_dir.is_dir() else [])
    return paths


def _summary(path: Path) -> dict[str, Any] | None:
    summary_path = path / "summary.json"
    if not summary_path.is_file():
        return None
    payload = _read_json(summary_path)
    return payload if isinstance(payload, dict) else None


def _bundle_is_complete(path: Path, *, benchmark_version: str, model: str) -> bool:
    if any(not (path / name).is_file() for name in REQUIRED_ARTIFACTS):
        return False
    manifest = _read_json(path / "manifest.json")
    summary = _summary(path)
    return (
        isinstance(manifest, dict)
        and isinstance(summary, dict)
        and manifest.get("benchmarkVersion") == benchmark_version
        and manifest.get("model") == model
        and summary.get("benchmarkVersion") == benchmark_version
        and summary.get("model") == model
        and summary.get("runValidity") == "valid"
        and summary.get("termination") == "episode_complete"
    )


def _latest_bundle(case_dir: Path) -> tuple[Path, dict[str, Any] | None]:
    latest: tuple[Path, dict[str, Any] | None] = (case_dir, None)
    for path in _attempt_dirs(case_dir):
        current = _summary(path)
        if current is not None or path == case_dir:
            latest = (path, current)
    return latest


def _next_attempt(case_dir: Path) -> tuple[int, Path]:
    numbers = [1]
    for path in case_dir.glob("attempt-*") if case_dir.is_dir() else ():
        try:
            numbers.append(int(path.name.removeprefix("attempt-")))
        except ValueError:
            continue
    attempt = max(numbers) + (0 if not case_dir.exists() else 1)
    return attempt, case_dir if attempt == 1 else case_dir / f"attempt-{attempt:02d}"


async def _run_episode(
    model: str, case_id: str, out_dir: Path, attempt_number: int, config: SweepConfig
) -> None:
    if config.benchmark_version == FIXED_WORKER_BENCHMARK_VERSION_V2:
        assert config.engine_repo is not None
        await run_fixed_workers_v2(
            FixedWorkersV2Config(
                case_id=case_id,
                model=model,
                engine_repo=config.engine_repo,
                max_attempts=config.retry_budget + 1,
                max_output_tokens=config.max_output_tokens,
                request_timeout=config.request_timeout,
                nim_api_keys_file=config.nim_api_keys_file,
                openrouter_api_key_file=config.openrouter_api_key_file,
            ),
            out_dir,
            episode_id=f"{config.benchmark_version}/{model}/{case_id}/{attempt_number}",
            attempt_number=attempt_number,
        )
        return
    runner_config = RunnerConfig(
        case_id=case_id,
        model=model,
        max_attempts=config.retry_budget + 1,
        max_output_tokens=config.max_output_tokens,
        max_turns=config.max_turns,
        request_timeout=config.request_timeout,
    )
    episode_id = f"{config.benchmark_version}/{model}/{case_id}/{attempt_number}"
    await _run_once(
        runner_config,
        out_dir,
        repeat=attempt_number,
        repeats=attempt_number,
        overwrite=False,
        benchmark_version=config.benchmark_version,
        episode_id=episode_id,
        attempt_number=attempt_number,
    )


def _metric(grade: Mapping[str, Any], name: str) -> float | None:
    aliases = {name, name.lower(), name[0].lower() + name[1:]}
    for block_name in ("scores", "benchmarkMetrics", "metrics"):
        block = grade.get(block_name)
        if isinstance(block, Mapping):
            for alias in aliases:
                value = block.get(alias)
                if isinstance(value, (int, float)):
                    return float(value)
    if name == "Safety":
        outcome = grade.get("outcome")
        safety = outcome.get("safety") if isinstance(outcome, Mapping) else None
        if isinstance(safety, (int, float)):
            return float(safety)
    return None


def _mean(values: Sequence[float | None]) -> float | None:
    present = [value for value in values if value is not None]
    return None if not present else sum(present) / len(present)


def _all_requirements(grade: Mapping[str, Any]) -> bool | None:
    for block in (grade, grade.get("benchmarkMetrics"), grade.get("metrics")):
        if isinstance(block, Mapping):
            for key in ("allRequirementsMet", "all_requirements_met"):
                if isinstance(block.get(key), bool):
                    return block[key]
    return None


def _model_report(model: str, entries: Sequence[dict[str, Any]], benchmark_version: str) -> dict[str, Any]:
    valid = [
        entry
        for entry in entries
        if entry.get("summary", {}).get("runValidity") == "valid"
        and entry.get("summary", {}).get("termination") == "episode_complete"
    ]
    served = sorted({served for entry in valid for served in entry.get("summary", {}).get("servedModels", [])})
    grades = [entry["grade"] for entry in valid if isinstance(entry.get("grade"), Mapping)]
    calls = sum(int(entry.get("summary", {}).get("turns", 0)) for entry in entries)
    requirements = [_all_requirements(grade) for grade in grades]
    met = [value for value in requirements if value is not None]
    publishable = len(valid) >= 8
    scores: dict[str, float | str | None] = {
        name: _mean([_metric(grade, name) for grade in grades])
        for name in ("Service", "Resources", "Recovery", "Safety", "Composite")
    }
    if not publishable:
        scores = {name: "N/A" for name in scores}
    return {
        "model": model,
        "served_model_id": served[0] if len(served) == 1 else None,
        "benchmark_version": benchmark_version,
        "planned_episodes": len(entries),
        "valid_episodes": len(valid),
        "provider_invalid_episodes": len(entries) - len(valid),
        **scores,
        "all_requirements_met_percent": None if not publishable or not met else 100 * sum(met) / len(met),
        "publishable": publishable,
        "total_model_calls": calls,
    }


async def run_sweep(config: SweepConfig, *, episode_runner: EpisodeRunner | None = None) -> dict[str, Any]:
    models, cases = validate_sweep_inputs(config)
    if config.dry_run:
        return {
            "dry_run": True,
            "benchmark_version": config.benchmark_version,
            "models": list(models),
            "cases": list(cases),
            "planned_episodes": len(models) * len(cases),
        }

    runner = episode_runner or _run_episode
    semaphore = asyncio.Semaphore(config.max_concurrency)

    async def run_model(model: str) -> dict[str, Any]:
        async with semaphore:
            entries: list[dict[str, Any]] = []
            model_dir = config.output_dir / _component(model)
            for case_id in cases:
                case_dir = model_dir / _component(case_id)
                latest_dir, latest_summary = _latest_bundle(case_dir)
                if _bundle_is_complete(latest_dir, benchmark_version=config.benchmark_version, model=model) or latest_summary and latest_summary.get("runValidity") == "provider_invalid" and not config.rerun_provider_invalid:
                    selected = latest_dir
                else:
                    attempt_number, selected = _next_attempt(case_dir)
                    await runner(model, case_id, selected, attempt_number, config)
                summary = _summary(selected) or {}
                grade = _read_json(selected / "grade.json") if (selected / "grade.json").is_file() else {}
                entries.append({"summary": summary, "grade": grade})
            return _model_report(model, entries, config.benchmark_version)

    reports = await asyncio.gather(*(run_model(model) for model in models))
    report = {
        "benchmark_version": config.benchmark_version,
        "planned_episodes": len(models) * len(cases),
        "models": reports,
    }
    config.output_dir.mkdir(parents=True, exist_ok=True)
    (config.output_dir / "sweep-report.json").write_text(json.dumps(report, indent=2, sort_keys=True) + "\n")
    return report


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Run a bounded held-out E-Sim model sweep.")
    parser.add_argument("--benchmark-version", required=True)
    parser.add_argument("--case-registry", dest="registry_path", required=True, type=Path)
    parser.add_argument("--model-roster", dest="roster_path", required=True, type=Path)
    parser.add_argument("--output-dir", required=True, type=Path)
    parser.add_argument("--max-concurrency", type=int, default=2)
    parser.add_argument("--request-timeout", type=float, default=30.0)
    parser.add_argument("--retry-budget", type=int, default=2)
    parser.add_argument("--max-output-tokens", type=int, default=2500)
    parser.add_argument("--max-turns", type=int, default=None)
    parser.add_argument("--rerun-provider-invalid", action="store_true")
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--engine-repo", type=Path)
    parser.add_argument("--nim-api-keys-file", type=Path)
    parser.add_argument("--openrouter-api-key-file", type=Path)
    parser.add_argument("--models", type=lambda value: tuple(part.strip() for part in value.split(",") if part.strip()), default=())
    parser.add_argument("--case-ids", type=lambda value: tuple(part.strip() for part in value.split(",") if part.strip()), default=())
    return parser


async def _main(args: argparse.Namespace) -> int:
    report = await run_sweep(SweepConfig(**vars(args)))
    print(json.dumps(report, indent=2, sort_keys=True))
    return 0


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        return asyncio.run(_main(args))
    except (CaseError, ValueError) as exc:
        print(f"sweep configuration error: {exc}")
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
