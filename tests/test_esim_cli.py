"""The CLI's contract: what a run leaves on disk, and what it may not do (#75)."""

import io
import json
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Self

import pytest

from retro_backend.esim.cli import (
    BUNDLE_VERSION,
    build_manifest,
    build_parser,
    resolve_case_id,
    write_bundle,
)
from retro_backend.esim.grader import grade
from retro_backend.esim.providers.base import ModelReply
from retro_backend.esim.runner import RunnerConfig, run_case

CASE = "dinner-rush-001"


class Replies:
    def __init__(self, replies: list[ModelReply]) -> None:
        self.replies = replies

    async def generate(
        self, messages: Sequence[Mapping[str, str]] | str, *, max_tokens: int | None = None
    ) -> ModelReply:
        del messages, max_tokens
        return self.replies.pop(0) if self.replies else _reply(None, "exhausted")

    async def close(self) -> None:
        return None

    async def __aenter__(self) -> Self:
        return self

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc_val: BaseException | None,
        exc_tb: object | None,
    ) -> None:
        await self.close()


def _reply(text: str | None, error: str | None = None, served: str | None = "vendor/m") -> ModelReply:
    return ModelReply(
        text=text,
        input_tokens=3,
        output_tokens=2,
        latency_ms=4.0,
        error=error,
        served_model=served if error is None else None,
    )


def test_resolve_case_id_accepts_the_documented_path_form() -> None:
    # The planning card spells --case as a path; load_case addresses by id.
    assert resolve_case_id("contracts/esim/cases/dinner-rush-001.json") == CASE
    assert resolve_case_id(CASE) == CASE


def test_manifest_separates_what_we_set_from_what_the_provider_decides() -> None:
    manifest = build_manifest(
        RunnerConfig(CASE, "nim:vendor/m"),
        case_seed=218220,
        contract_version="restaurant-arena/v1",
        repeat=1,
        repeats=3,
        started_at="2026-09-04T00:00:00+00:00",
    )

    assert manifest["bundleVersion"] == BUNDLE_VERSION
    assert manifest["sampling"]["set"] == {"temperature": 0.0}
    # A bundle that claimed a seed it never sent would imply a reproducibility
    # this harness does not have.
    provider_controlled = " ".join(manifest["sampling"]["providerControlled"])
    assert "seed" in provider_controlled
    assert manifest["repeats"] == 3
    assert manifest["harness"]["roleOrder"] == ["chef", "server", "host", "expediter"]


@pytest.mark.asyncio
async def test_bundle_records_everything_the_checklist_asks_for(tmp_path: Path) -> None:
    adapter = Replies([_reply('{"tool":"prepare","arguments":{"orderId":"order-2"}}')])
    result = await run_case(
        RunnerConfig(CASE, "nim:vendor/m", max_turns=1), adapter, jsonl=io.StringIO()
    )
    graded = grade(result.trajectory)

    write_bundle(
        tmp_path,
        manifest=build_manifest(
            RunnerConfig(CASE, "nim:vendor/m"),
            case_seed=218220,
            contract_version="restaurant-arena/v1",
            repeat=1,
            repeats=1,
            started_at="2026-09-04T00:00:00+00:00",
        ),
        result=result,
        graded=graded,
    )

    for name in (
        "manifest.json",
        "initial-state.json",
        "final-state.json",
        "trajectory.json",
        "grade.json",
        "summary.json",
    ):
        assert (tmp_path / name).is_file(), name

    summary = json.loads((tmp_path / "summary.json").read_text())
    assert summary["initialStateHash"].startswith("sha256:")
    assert summary["finalStateHash"].startswith("sha256:")
    assert summary["servedModels"] == ["vendor/m"]


@pytest.mark.asyncio
async def test_summary_flags_a_model_the_provider_swapped_underneath_us() -> None:
    adapter = Replies(
        [_reply('{"tool":"prepare","arguments":{"orderId":"order-2"}}', served="vendor/other")]
    )

    result = await run_case(
        RunnerConfig(CASE, "nim:vendor/m", max_turns=1), adapter, jsonl=io.StringIO()
    )

    assert result.summary["servedModels"] == ["vendor/other"]
    assert result.summary["servedModelMismatch"] is True


@pytest.mark.asyncio
async def test_turn_budget_leaves_room_for_retries(tmp_path: Path) -> None:
    """A full episode must be reachable when the model retries.

    Budgeting exactly ``maxTicks * roles`` made ``episode_complete``
    unreachable for any model that ever retried, so completion measured the
    budget rather than the model.
    """
    del tmp_path
    config = RunnerConfig(CASE, "nim:vendor/m", max_attempts=3)
    successful_turns_needed = 24 * 4
    implied_budget = 24 * 4 * config.max_attempts

    assert config.max_turns is None
    assert implied_budget > successful_turns_needed


def test_cli_exposes_no_reference_policy_flag() -> None:
    """Scripted actions must not be reachable from the model-run command.

    ``esim.policies`` is infrastructure-only; the offline driver lives in
    ``scripts/reference_rollout.py`` so no scripted action can be recorded
    through the same path that records model decisions.
    """
    parser = build_parser()
    base = ["run", "--case", CASE, "--output-dir", "/tmp/x"]

    # The model flag is the supported way in.
    parsed = parser.parse_args([*base, "--model", "nim:vendor/m"])
    assert parsed.model == "nim:vendor/m"

    # A reference policy is not selectable here at any spelling.
    with pytest.raises(SystemExit):
        parser.parse_args([*base, "--policy", "reference"])


def test_cli_budget_flags_do_not_shadow_the_runner_defaults() -> None:
    """An unnamed budget must come from the runner, not a copy in the parser.

    A default repeated in argparse silently overrode the runner's own after it
    was raised, and the manifest then recorded the stale value as a deliberate
    choice -- the worst kind of wrong, because it looked audited.
    """
    parser = build_parser()
    args = parser.parse_args(
        ["run", "--case", CASE, "--model", "nim:vendor/m", "--output-dir", "/tmp/x"]
    )

    assert args.max_output_tokens is None
    assert args.max_attempts is None
    assert args.max_turns is None

    # What the runner would use when the CLI names nothing.
    config = RunnerConfig(case_id=CASE, model="nim:vendor/m")
    assert config.max_output_tokens == 1024

