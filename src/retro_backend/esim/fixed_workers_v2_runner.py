"""Live model runner for the frozen TypeScript fixed-workers v2 environment."""

import asyncio
import json
import tempfile
from collections import Counter
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any, TextIO, cast

from retro_backend.esim.case import load_fixed_worker_case_v2
from retro_backend.esim.grader import (
    BenchmarkEpisodeV2,
    grade_benchmark_events_v2,
    verify_benchmark_replay_v2,
)
from retro_backend.esim.providers.base import ERR_RATE_LIMIT, ERR_TIMEOUT, ModelAdapter, ModelReply
from retro_backend.esim.providers.nim import NimAdapter
from retro_backend.esim.providers.openrouter import OpenRouterAdapter
from retro_backend.esim.providers.registry import get_adapter, parse_model_spec
from retro_backend.esim.rotation import retryable_model_error
from retro_backend.esim.runner import record_jsonl

BUNDLE_VERSION = "esim-fixed-workers-v2-bundle/v2"


@dataclass(frozen=True, slots=True)
class FixedWorkersV2Config:
    case_id: str
    model: str
    engine_repo: Path
    max_attempts: int = 3
    max_output_tokens: int = 8000
    request_timeout: float = 30.0
    nim_api_keys_file: Path | None = None
    openrouter_api_key_file: Path | None = None
    openrouter_provider_order: tuple[str, ...] = ()
    openrouter_reasoning_effort: str | None = None


class _RotatingAdapter:
    def __init__(self, adapters: list[ModelAdapter]) -> None:
        self.adapters = adapters
        self.index = 0
        self.lock = asyncio.Lock()

    async def generate(
        self, messages: Sequence[Mapping[str, str]] | str, *, max_tokens: int | None = None
    ) -> ModelReply:
        async with self.lock:
            adapter = self.adapters[self.index % len(self.adapters)]
            self.index += 1
        return await adapter.generate(messages, max_tokens=max_tokens)

    async def close(self) -> None:
        await asyncio.gather(*(adapter.close() for adapter in self.adapters))

    async def __aenter__(self) -> "_RotatingAdapter":
        return self

    async def __aexit__(self, exc_type: type[BaseException] | None, exc_val: BaseException | None, exc_tb: object | None) -> None:
        await self.close()


def _adapter(config: FixedWorkersV2Config) -> ModelAdapter:
    if config.nim_api_keys_file is None and config.openrouter_api_key_file is None:
        return get_adapter(config.model)
    provider, model = parse_model_spec(config.model)
    if config.openrouter_api_key_file is not None:
        if config.nim_api_keys_file is not None or provider != "openrouter":
            raise ValueError("--openrouter-api-key-file may only be used with openrouter: models")
        key_path = config.openrouter_api_key_file
        keys = [line.strip() for line in key_path.read_text().splitlines() if line.strip()]
        if len(keys) != 1:
            raise ValueError("OpenRouter key file must contain exactly one nonblank key")
        return OpenRouterAdapter(model, api_key=keys[0], timeout=config.request_timeout, provider_order=config.openrouter_provider_order, reasoning_effort=config.openrouter_reasoning_effort)
    if provider != "nim":
        raise ValueError("--nim-api-keys-file may only be used with nim: models")
    nim_key_path = cast(Path, config.nim_api_keys_file)
    keys = [line.strip() for line in nim_key_path.read_text().splitlines() if line.strip()]
    if not keys or len(keys) != len(set(keys)):
        raise ValueError("NIM key file must contain unique, nonblank keys")
    return _RotatingAdapter(
        [NimAdapter(model, api_key=key, timeout=config.request_timeout) for key in keys]
    )


def _messages(turn: Mapping[str, object]) -> list[dict[str, str]]:
    return [
        {
            "role": "system",
            "content": "You manage a deterministic restaurant shift. Return only strict JSON with one action from availableActions: {\"action\":\"...\"}.",
        },
        {"role": "user", "content": json.dumps(turn, sort_keys=True, separators=(",", ":"))},
    ]


def _retry_seconds(reply: ModelReply, attempt: int) -> float:
    if reply.retry_after_seconds is not None:
        return min(120.0, reply.retry_after_seconds)
    if reply.error and reply.error.startswith(ERR_RATE_LIMIT):
        return 60.0
    return min(30.0, 0.25 * (2 ** (attempt - 1)))


def _parse_action(text: str, available: list[str]) -> str:
    try:
        payload = json.loads(text)
    except json.JSONDecodeError as exc:
        raise ValueError(f"invalid_json: {exc.msg}") from exc
    if not isinstance(payload, dict) or set(payload) != {"action"} or payload["action"] not in available:
        raise ValueError("invalid_action: expected exactly one available action")
    return str(payload["action"])


async def _generate(
    adapter: ModelAdapter,
    config: FixedWorkersV2Config,
    turn: Mapping[str, object],
    attempts: TextIO,
    totals: Counter[str],
    served: set[str],
) -> tuple[str | None, str | None]:
    available = turn.get("availableActions")
    assert isinstance(available, list) and all(isinstance(value, str) for value in available)
    for attempt in range(1, config.max_attempts + 1):
        try:
            reply = await adapter.generate(_messages(turn), max_tokens=config.max_output_tokens)
        except TimeoutError:
            reply = ModelReply(None, 0, 0, config.request_timeout * 1000, f"{ERR_TIMEOUT}: request exceeded {config.request_timeout:g}s")
        totals["turns"] += 1
        totals["input_tokens"] += reply.input_tokens
        totals["output_tokens"] += reply.output_tokens
        totals["total_billed_tokens"] += reply.input_tokens + reply.output_tokens
        totals["latency_ms"] += round(reply.latency_ms)
        totals["queue_delay_ms"] += round(reply.queue_delay_ms)
        if reply.served_model:
            served.add(reply.served_model)
        observation = turn.get("observation")
        record: dict[str, object] = {
            "event": "model_attempt", "tick": observation.get("tick") if isinstance(observation, Mapping) else None,
            "kind": turn.get("kind"), "attempt": attempt, "servedModel": reply.served_model,
            "latencyMs": reply.latency_ms, "queueDelayMs": reply.queue_delay_ms,
            "inputTokens": reply.input_tokens, "outputTokens": reply.output_tokens,
        }
        if not reply.ok:
            error = reply.error or "unknown_model_error"
            record.update(error=error, providerError=True)
            record_jsonl(attempts, record)
            if retryable_model_error(error) and attempt < config.max_attempts:
                await asyncio.sleep(_retry_seconds(reply, attempt))
                continue
            return None, error
        record["response"] = reply.text
        try:
            action = _parse_action(reply.text or "", available)
        except ValueError as exc:
            totals["invalid_responses"] += 1
            record.update(error=str(exc), modelBehavior=True)
            record_jsonl(attempts, record)
            return None, None
        record["action"] = action
        record_jsonl(attempts, record)
        return action, None
    return None, "retry_budget_exhausted"


async def _handover(
    adapter: ModelAdapter,
    config: FixedWorkersV2Config,
    observation: Mapping[str, object],
    attempts: TextIO,
    totals: Counter[str],
    served: set[str],
) -> tuple[bool, dict[str, object] | None, str | None]:
    prompt = [
        {"role": "system", "content": "Report the terminal result. Return only strict JSON: {\"cashEarned\":number,\"targetReached\":boolean}."},
        {"role": "user", "content": json.dumps(observation, sort_keys=True, separators=(",", ":"))},
    ]
    for attempt in range(1, config.max_attempts + 1):
        try:
            reply = await adapter.generate(prompt, max_tokens=config.max_output_tokens)
        except TimeoutError:
            reply = ModelReply(None, 0, 0, config.request_timeout * 1000, f"{ERR_TIMEOUT}: request exceeded {config.request_timeout:g}s")
        totals["turns"] += 1
        totals["input_tokens"] += reply.input_tokens
        totals["output_tokens"] += reply.output_tokens
        totals["total_billed_tokens"] += reply.input_tokens + reply.output_tokens
        totals["latency_ms"] += round(reply.latency_ms)
        totals["queue_delay_ms"] += round(reply.queue_delay_ms)
        if reply.served_model:
            served.add(reply.served_model)
        record: dict[str, object] = {
            "event": "handover_attempt", "attempt": attempt, "servedModel": reply.served_model,
            "latencyMs": reply.latency_ms, "queueDelayMs": reply.queue_delay_ms,
            "inputTokens": reply.input_tokens, "outputTokens": reply.output_tokens,
        }
        if not reply.ok:
            error = reply.error or "unknown_model_error"
            record.update(error=error, providerError=True)
            record_jsonl(attempts, record)
            if retryable_model_error(error) and attempt < config.max_attempts:
                await asyncio.sleep(_retry_seconds(reply, attempt))
                continue
            return False, None, error
        record["response"] = reply.text
        try:
            payload = json.loads(reply.text or "")
        except json.JSONDecodeError as exc:
            payload = None
            record.update(error=f"invalid_json: {exc.msg}", modelBehavior=True)
        valid = (
            isinstance(payload, dict) and set(payload) == {"cashEarned", "targetReached"}
            and isinstance(payload.get("cashEarned"), (int, float))
            and not isinstance(payload.get("cashEarned"), bool)
            and isinstance(payload.get("targetReached"), bool)
        )
        if not valid:
            totals["invalid_responses"] += 1
            record.setdefault("error", "invalid_handover: expected cashEarned number and targetReached boolean")
            record_jsonl(attempts, record)
            return False, payload if isinstance(payload, dict) else None, None
        assert isinstance(payload, dict)
        accurate = payload["cashEarned"] == observation.get("cashEarned") and payload["targetReached"] is observation.get("targetReached")
        record["accurate"] = accurate
        record_jsonl(attempts, record)
        return accurate, payload, None
    return False, None, "retry_budget_exhausted"


async def _replay_with_engine(
    config: FixedWorkersV2Config, case_id: str, seed: int, actions: Sequence[Mapping[str, object]]
) -> Mapping[str, object]:
    with tempfile.NamedTemporaryFile(mode="w", suffix=".json", encoding="utf-8", delete=False) as replay_file:
        json.dump(actions, replay_file, separators=(",", ":"))
        replay_path = Path(replay_file.name)
    try:
        process = await asyncio.create_subprocess_exec(
            "npx", "--yes", "bun@1.3.11", str(Path(__file__).resolve().parents[3] / "scripts/fixed_workers_v2_bridge.ts"),
            str(config.engine_repo), load_fixed_worker_case_v2(case_id).level, str(seed), str(replay_path),
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE, limit=10 * 1024 * 1024,
        )
        stdout, stderr = await process.communicate()
        if process.returncode:
            raise RuntimeError(f"v2 replay bridge failed ({process.returncode}): {stderr.decode().strip()}")
        payload = json.loads(stdout.decode())
        if not isinstance(payload, Mapping) or payload.get("type") != "complete":
            raise RuntimeError("v2 replay bridge returned no completion record")
        return payload
    finally:
        replay_path.unlink(missing_ok=True)


async def run_fixed_workers_v2(
    config: FixedWorkersV2Config,
    out_dir: Path,
    *,
    adapter: ModelAdapter | None = None,
    episode_id: str,
    attempt_number: int,
) -> Mapping[str, object]:
    case = load_fixed_worker_case_v2(config.case_id)
    out_dir.mkdir(parents=True, exist_ok=False)
    attempts_path = out_dir / "attempts.jsonl"
    process = await asyncio.create_subprocess_exec(
        "npx", "--yes", "bun@1.3.11", str(Path(__file__).resolve().parents[3] / "scripts/fixed_workers_v2_bridge.ts"),
        str(config.engine_repo), case.level, str(case.seed),
        stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
        limit=10 * 1024 * 1024,
    )
    assert process.stdin and process.stdout
    model_adapter = adapter or _adapter(config)
    totals = Counter[str]()
    served: set[str] = set()
    provider_error: str | None = None
    completed: dict[str, Any] | None = None
    handover_accurate = False
    handover: dict[str, object] | None = None
    with attempts_path.open("w") as attempts:
        while line := await process.stdout.readline():
            message = json.loads(line)
            if message.get("type") == "complete":
                completed = message
                break
            action, error = (None, None) if provider_error else await _generate(
                model_adapter, config, message, attempts, totals, served
            )
            provider_error = provider_error or error
            process.stdin.write((json.dumps({"action": action}) + "\n").encode())
            await process.stdin.drain()
        if completed is not None and provider_error is None:
            handover_accurate, handover, error = await _handover(
                model_adapter, config, completed["terminalObservation"], attempts, totals, served
            )
            provider_error = provider_error or error
    process.stdin.close()
    stderr = (await process.stderr.read()).decode().strip() if process.stderr else ""
    return_code = await process.wait()
    if return_code or completed is None:
        raise RuntimeError(f"v2 engine bridge failed ({return_code}): {stderr}")

    scores = grade_benchmark_events_v2(completed["events"], case)
    try:
        replayed = await _replay_with_engine(config, case.case_id, case.seed, completed["actions"])
        replay_events = cast(Sequence[Mapping[str, object]], replayed["events"])
        replay_scores = grade_benchmark_events_v2(replay_events, case)
        replay_verified = (
            replayed.get("finalState") == completed.get("finalState")
            and verify_benchmark_replay_v2(replay_events, scores, case)
            and replay_scores == scores
        )
    except (RuntimeError, json.JSONDecodeError, KeyError):
        replay_verified = False
    requested = parse_model_spec(config.model)[1]
    identity_mismatch = bool(served) and served != {requested}
    run_validity = "provider_failure" if provider_error else "unverified_identity" if identity_mismatch or not served else "valid"
    episode = BenchmarkEpisodeV2(
        case_id=case.case_id, run_validity=run_validity, scores=scores,
        replay_verified=replay_verified, handover_accurate=handover_accurate,
    )
    completed["handover"] = handover
    manifest = {
        "bundleVersion": BUNDLE_VERSION, "benchmarkVersion": case.benchmark_version,
        "graderVersion": case.grader_version, "episodeId": episode_id,
        "attemptNumber": attempt_number, "caseId": case.case_id, "seed": case.seed,
        "model": config.model, "engineRepo": str(config.engine_repo.resolve()),
        "openrouterProviderOrder": list(config.openrouter_provider_order),
        "openrouterReasoningEffort": config.openrouter_reasoning_effort,
        "maxOutputTokens": config.max_output_tokens,
        "requestTimeoutSeconds": config.request_timeout,
    }
    grade = episode.model_dump(by_alias=True, mode="json") | {"allRequirementsMet": episode.all_requirements_met}
    summary = {
        "benchmarkVersion": case.benchmark_version, "caseId": case.case_id, "model": config.model,
        "servedModels": sorted(served), "servedModelMismatch": identity_mismatch,
        "runValidity": run_validity, "providerError": provider_error,
        "termination": "episode_complete", "ticks": case.duration_ticks,
        "replayVerified": replay_verified,
        "requestedProviderRoute": list(config.openrouter_provider_order),
        "reasoningEffort": config.openrouter_reasoning_effort,
        "maxOutputTokens": config.max_output_tokens,
        **totals,
    }
    for name, payload in {
        "manifest.json": manifest,
        "trajectory.json": completed,
        "grade.json": grade,
        "summary.json": summary,
    }.items():
        (out_dir / name).write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n")
    return summary
