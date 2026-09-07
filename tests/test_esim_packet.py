"""The evidence packet's contract: what ships, what stays, what must reproduce (#80).

The packet is assembled before #81 has run the matrix, so the first thing these
tests pin is that an empty matrix packages cleanly and says so. The rest pin the
two properties the packet exists for: a shared row re-grades to the number
printed beside it, and the raw attempt log never leaves with it.
"""

import json
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any, Self

import pytest

from retro_backend.esim.cli import main
from retro_backend.esim.packet import (
    PACKET_VERSION,
    PRIVATE_FILES,
    PacketError,
    PacketExistsError,
    PacketPlan,
    load_plan,
    row_id,
    write_packet,
)
from retro_backend.esim.providers.base import ERR_TIMEOUT, ModelAdapter, ModelReply
from retro_backend.esim.providers.registry import register_provider

CASE = "dinner-rush-001"
PLAN_PATH = Path("contracts/esim/packets/m5-demo.json")


class _Replies:
    """A stub adapter that answers from a script; no key, no network."""

    def __init__(self, replies: Sequence[ModelReply], *, served: str = "vendor/m") -> None:
        self.replies = list(replies)
        self.served = served

    async def generate(
        self, messages: Sequence[Mapping[str, str]] | str, *, max_tokens: int | None = None
    ) -> ModelReply:
        del messages, max_tokens
        if self.replies:
            return self.replies.pop(0)
        return _reply('{"tool":null}', served=self.served)

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


def _reply(
    text: str | None, error: str | None = None, served: str | None = "vendor/m"
) -> ModelReply:
    return ModelReply(
        text=text,
        input_tokens=3,
        output_tokens=2,
        latency_ms=4.0,
        error=error,
        served_model=served if error is None else None,
    )


def _plan(**overrides: object) -> PacketPlan:
    base: dict[str, object] = {
        "packetId": "test-packet",
        "workerModel": "stub:vendor/staff",
        "rounds": 2,
        "levels": [CASE],
        "managerModels": ["stub:vendor/boss"],
    }
    return PacketPlan.model_validate({**base, **overrides})


def _run_a_managed_bundle(out: Path, *, manager_fails: bool = False) -> None:
    """Produce one real managed bundle from stub providers."""

    def _stub(model: str, **_: object) -> ModelAdapter:
        if model.endswith("boss"):
            if manager_fails:
                return _Replies([_reply(None, ERR_TIMEOUT)] * 8, served=model)
            return _Replies(
                [
                    _reply(
                        '{"tool":"set_directive","arguments":'
                        + '{"targetRole":"chef","instruction":"verify order-2"}}',
                        served=model,
                    )
                ],
                served=model,
            )
        return _Replies([], served=model)

    register_provider("stub", _stub)
    assert (
        main(
            [
                "run-managed",
                "--case",
                CASE,
                "--manager-model",
                "stub:vendor/boss",
                "--worker-model",
                "stub:vendor/staff",
                "--rounds",
                "2",
                "--output-dir",
                str(out),
            ]
        )
        == 0
    )


def _package(plan: PacketPlan, tmp_path: Path, **overrides: object) -> dict[str, Any]:
    kwargs: dict[str, object] = {
        "runs_root": tmp_path,
        "out_dir": tmp_path / "packet",
        "repo": Path("."),
    }
    return write_packet(plan, **{**kwargs, **overrides})  # pyright: ignore[reportArgumentType]


# -- the skeleton, before any run exists --------------------------------


def test_an_unrun_matrix_packages_and_reports_every_row_as_planned(tmp_path: Path) -> None:
    """The state #80 is reviewed in: the shell is complete, the rows are not.

    A packet that refused to build until the matrix was finished would put the
    packaging, the limitations, and the sanitisation split behind #81 instead
    of beside it.
    """
    plan = _plan(levels=[CASE, "quiet-tuesday-001"], managerModels=["stub:a", "stub:b"])
    manifest = _package(plan, tmp_path)

    assert manifest["packetVersion"] == PACKET_VERSION
    assert manifest["counts"] == {
        "planned": 4,
        "completed": 0,
        "invalid": 0,
        "mismatched": 0,
        "redacted": 0,
        "incomplete": 0,
        "missingBundle": 0,
        "notYetRun": 4,
    }
    report = (tmp_path / "packet" / "sanitized" / "REPORT.md").read_text()
    assert "**Placeholder report.** 0 of 4 planned rows" in report
    # Every planned cell is printed with the command that produces it, so the
    # matrix is auditable for coverage before a single run has been made.
    for case_id in (CASE, "quiet-tuesday-001"):
        for model in ("stub:a", "stub:b"):
            assert row_id(case_id, model) in report
            assert f"--manager-model {model}" in report
    assert "--" in report and "0.0" not in report


def test_the_skeleton_carries_a_task_card_limitations_and_a_private_index(
    tmp_path: Path,
) -> None:
    _package(_plan(), tmp_path)
    sanitized = tmp_path / "packet" / "sanitized"

    card = (sanitized / "TASK-CARD.md").read_text()
    assert "Fixed worker model: `stub:vendor/staff`" in card
    assert "1 levels x 1 managers = 1 rows" in card

    limits = (sanitized / "LIMITATIONS.md").read_text()
    for required in (
        "One run per model per level",
        "Live worker variation",
        "Verified roster and coverage",
        "Free-prototype constraints",
        "No equivalence or improvement claim",
        "Sanitised and private",
    ):
        assert required in limits
    assert "APEX-equivalent" in limits

    index = json.loads((tmp_path / "packet" / "private" / "source-index.json").read_text())
    assert index["withheld"] == list(PRIVATE_FILES)
    assert index["rows"][0]["sourceBundle"] is None


def test_the_checked_in_demo_plan_loads_and_packages(tmp_path: Path) -> None:
    """The frozen plan in ``contracts/`` is the one the demo is cut from."""
    plan = load_plan(PLAN_PATH)
    assert plan.status == "skeleton"
    assert len(plan.levels) == 3
    assert plan.excluded_models, "an excluded model must be recorded, not dropped"

    manifest = _package(plan, tmp_path)
    assert manifest["counts"]["planned"] == 6
    assert manifest["repositoryRevision"]


def test_a_plan_this_build_cannot_read_names_the_file(tmp_path: Path) -> None:
    bad = tmp_path / "plan.json"
    bad.write_text('{"packetId": "x"}')
    with pytest.raises(PacketError, match="does not match esim-packet-plan/v1"):
        load_plan(bad)
    with pytest.raises(PacketError, match="not valid JSON"):
        (tmp_path / "broken.json").write_text("{")
        load_plan(tmp_path / "broken.json")


# -- a row with evidence ------------------------------------------------


def test_a_completed_row_ships_its_evidence_and_regrades_from_the_shared_copy(
    tmp_path: Path,
) -> None:
    """The acceptance criterion, executed: the shared copy reproduces its grade.

    The re-grade runs against the file inside ``sanitized/``, not against the
    source bundle, because the shared copy is the only thing an unfamiliar
    reader has.
    """
    _run_a_managed_bundle(tmp_path / "runs" / "row-1")
    identifier = row_id(CASE, "stub:vendor/boss")
    manifest = _package(_plan(bundles={identifier: "runs/row-1"}), tmp_path)

    row = manifest["rows"][0]
    assert row["status"] == "complete"
    assert row["runValidity"] == "valid"
    assert row["regradeMatches"] is True
    assert row["costUsd"] is None, "an unmeasured cost must not be reported as zero"

    shared = tmp_path / "packet" / "sanitized" / "runs" / identifier
    for name in ("manifest.json", "trajectory.json", "grade.json", "summary.json", "directives.json"):
        assert (shared / name).exists()

    # The documented command, run from the packet, with no provider registered
    # for the model the bundle names.
    assert main(["regrade", "--trajectory", str(shared / "trajectory.json")]) == 0
    assert row["regradeCommand"] == (
        "uv run python -m retro_backend.esim.cli regrade"
        f" --trajectory runs/{identifier}/trajectory.json"
    )

    report = (tmp_path / "packet" / "sanitized" / "REPORT.md").read_text()
    assert "Placeholder report" not in report
    assert f"runs/{identifier}/grade.json" in report
    assert "reproduces" in report


def test_the_raw_attempt_log_stays_behind_and_is_indexed_not_copied(tmp_path: Path) -> None:
    """The split: every shared artifact is digest-matched, the raw log is not shared."""
    _run_a_managed_bundle(tmp_path / "runs" / "row-1")
    identifier = row_id(CASE, "stub:vendor/boss")
    source = tmp_path / "runs" / "row-1"
    manifest = _package(_plan(bundles={identifier: "runs/row-1"}), tmp_path)

    shared = tmp_path / "packet" / "sanitized" / "runs" / identifier
    assert (source / "attempts.jsonl").exists()
    assert not (shared / "attempts.jsonl").exists()

    index = json.loads((tmp_path / "packet" / "private" / "source-index.json").read_text())
    private = index["rows"][0]["privateArtifacts"]
    assert [record["file"] for record in private] == ["attempts.jsonl"]
    assert private[0]["path"] == str(source.resolve())+"/attempts.jsonl"
    assert len(private[0]["sha256"]) == 64

    # Nothing was rewritten on the way out: the shared digest is the source's.
    for record in manifest["rows"][0]["artifacts"]:
        assert record["sourceSha256"] == record["sanitizedSha256"]
        assert record["redactions"] == 0
    shared_digests = {
        record["file"]: record["sourceSha256"]
        for record in index["rows"][0]["sharedArtifacts"]
    }
    assert shared_digests["grade.json"]


def test_a_credential_in_a_run_record_is_redacted_and_the_difference_is_visible(
    tmp_path: Path,
) -> None:
    """Defence in depth, and it is reported rather than silently applied."""
    source = tmp_path / "runs" / "leaky"
    source.mkdir(parents=True)
    (source / "manifest.json").write_text('{"endpoint": "https://x/v1", "key": "nvapi-abcd1234efgh"}')
    identifier = row_id(CASE, "stub:vendor/boss")
    manifest = _package(_plan(bundles={identifier: "runs/leaky"}), tmp_path)

    shared = tmp_path / "packet" / "sanitized" / "runs" / identifier / "manifest.json"
    assert "nvapi-" not in shared.read_text()
    assert "[redacted-api-key]" in shared.read_text()
    record = manifest["rows"][0]["artifacts"][0]
    assert record["redactions"] == 1
    assert record["sourceSha256"] != record["sanitizedSha256"]
    # The bundle was a stub with only one file, so the row is incomplete: a
    # missing artifact outranks a redaction, and either way it does not ship.
    assert manifest["rows"][0]["status"] == "incomplete"


# -- rows that must not read as results ---------------------------------


def test_a_degraded_run_is_reported_invalid_rather_than_scored(tmp_path: Path) -> None:
    """A run the provider broke is not a statement about the manager model."""
    _run_a_managed_bundle(tmp_path / "runs" / "row-1", manager_fails=True)
    identifier = row_id(CASE, "stub:vendor/boss")
    manifest = _package(_plan(bundles={identifier: "runs/row-1"}), tmp_path)

    row = manifest["rows"][0]
    assert row["status"] == "invalid"
    assert row["runValidity"] == "provider_failure"
    assert manifest["counts"]["invalid"] == 1
    assert manifest["counts"]["completed"] == 0
    report = (tmp_path / "packet" / "sanitized" / "REPORT.md").read_text()
    assert "provider_failure" in report


def test_a_grade_that_no_longer_reproduces_is_flagged_not_published(tmp_path: Path) -> None:
    """The "nothing is edited to pass" check, exercised against an edited grade."""
    _run_a_managed_bundle(tmp_path / "runs" / "row-1")
    graded = json.loads((tmp_path / "runs" / "row-1" / "grade.json").read_text())
    assert graded["outcome"]["orderCompletion"] == 0.0
    graded["outcome"]["orderCompletion"] = 1.0
    (tmp_path / "runs" / "row-1" / "grade.json").write_text(json.dumps(graded))

    identifier = row_id(CASE, "stub:vendor/boss")
    manifest = _package(_plan(bundles={identifier: "runs/row-1"}), tmp_path)
    row = manifest["rows"][0]
    assert row["status"] == "invalid"
    assert row["regradeMatches"] is False
    assert "differs from recorded grade" in row["regradeDetail"]


def test_a_bundle_path_that_does_not_exist_is_missing_not_planned(tmp_path: Path) -> None:
    """"Not run yet" and "the run's evidence is gone" are different failures."""
    identifier = row_id(CASE, "stub:vendor/boss")
    manifest = _package(_plan(bundles={identifier: "runs/gone"}), tmp_path)
    row = manifest["rows"][0]
    assert row["status"] == "missing"
    assert manifest["counts"]["missingBundle"] == 1
    assert manifest["counts"]["notYetRun"] == 0


def test_a_bundle_missing_a_required_file_is_incomplete(tmp_path: Path) -> None:
    _run_a_managed_bundle(tmp_path / "runs" / "row-1")
    (tmp_path / "runs" / "row-1" / "grade.json").unlink()
    identifier = row_id(CASE, "stub:vendor/boss")
    manifest = _package(_plan(bundles={identifier: "runs/row-1"}), tmp_path)
    row = manifest["rows"][0]
    assert row["status"] == "incomplete"
    assert row["missingFiles"] == ["grade.json"]
    assert row["regradeMatches"] is None


# -- rebuilding ---------------------------------------------------------


def test_a_rebuild_refuses_to_land_on_an_existing_packet(tmp_path: Path) -> None:
    _package(_plan(), tmp_path)
    with pytest.raises(PacketExistsError, match="already holds a packet"):
        _package(_plan(), tmp_path)


def test_an_overwrite_drops_the_rows_the_new_plan_no_longer_names(tmp_path: Path) -> None:
    """A stale row directory would outlive the report that referenced it."""
    _run_a_managed_bundle(tmp_path / "runs" / "row-1")
    identifier = row_id(CASE, "stub:vendor/boss")
    _package(_plan(bundles={identifier: "runs/row-1"}), tmp_path)
    assert (tmp_path / "packet" / "sanitized" / "runs" / identifier).exists()

    _package(_plan(managerModels=["stub:other"]), tmp_path, overwrite=True)
    assert not (tmp_path / "packet" / "sanitized" / "runs" / identifier).exists()


def test_the_package_subcommand_builds_the_checked_in_plan(tmp_path: Path) -> None:
    argv = [
        "package",
        "--plan",
        str(PLAN_PATH),
        "--runs-root",
        str(tmp_path),
        "--output-dir",
        str(tmp_path / "packet"),
    ]
    assert main(argv) == 0
    assert (tmp_path / "packet" / "sanitized" / "REPORT.md").exists()
    # A second build over the same directory is refused, as a run bundle is.
    assert main(argv) == 3
    assert main([*argv, "--overwrite"]) == 0


def test_a_redacted_row_is_withheld_until_a_human_has_seen_the_source(tmp_path: Path) -> None:
    """The shared copy is clean; the source bundle still needs a look.

    Redacting and then publishing normally would leave the only trace of a
    leaked credential in per-artifact metadata nobody reads.
    """
    _run_a_managed_bundle(tmp_path / "runs" / "row-1")
    summary_path = tmp_path / "runs" / "row-1" / "summary.json"
    leaked = json.loads(summary_path.read_text())
    leaked["endpoint"] = "https://integrate.api.nvidia.com/v1 key=nvapi-abcd1234efgh"
    summary_path.write_text(json.dumps(leaked))

    identifier = row_id(CASE, "stub:vendor/boss")
    manifest = _package(_plan(bundles={identifier: "runs/row-1"}), tmp_path)
    row = manifest["rows"][0]

    assert row["status"] == "redacted"
    assert row["redactions"] == 1
    assert row["publishedNumbers"] is False
    assert manifest["counts"]["redacted"] == 1
    assert manifest["counts"]["completed"] == 0
    shared = tmp_path / "packet" / "sanitized" / "runs" / identifier / "summary.json"
    assert "nvapi-" not in shared.read_text()


def test_a_bundle_from_another_cell_is_mismatched_not_published(tmp_path: Path) -> None:
    """A stale path in `bundles` re-grades perfectly and still is not this row."""
    _run_a_managed_bundle(tmp_path / "runs" / "row-1")
    plan = _plan(levels=["quiet-tuesday-001"], bundles={
        row_id("quiet-tuesday-001", "stub:vendor/boss"): "runs/row-1"
    })
    manifest = _package(plan, tmp_path)
    row = manifest["rows"][0]

    assert row["status"] == "mismatched"
    assert row["regradeMatches"] is True, "the run is intact; it just is not this row's"
    assert any("caseId" in item for item in row["identityMismatches"])
    assert manifest["counts"]["mismatched"] == 1


def test_a_mismatched_worker_model_or_round_count_is_caught_too(tmp_path: Path) -> None:
    _run_a_managed_bundle(tmp_path / "runs" / "row-1")
    identifier = row_id(CASE, "stub:vendor/boss")
    manifest = _package(
        _plan(rounds=4, workerModel="stub:vendor/other", bundles={identifier: "runs/row-1"}),
        tmp_path,
    )
    mismatches = manifest["rows"][0]["identityMismatches"]
    assert any("workerModel" in item for item in mismatches)
    assert any("budgets.rounds" in item for item in mismatches)


def test_a_row_that_is_not_complete_publishes_no_numbers(tmp_path: Path) -> None:
    """Failures stay visible; their figures do not become results."""
    _run_a_managed_bundle(tmp_path / "runs" / "row-1", manager_fails=True)
    identifier = row_id(CASE, "stub:vendor/boss")
    _package(_plan(bundles={identifier: "runs/row-1"}), tmp_path)

    report = (tmp_path / "packet" / "sanitized" / "REPORT.md").read_text()
    matrix_row = next(line for line in report.split("\n") if line.startswith(f"| `{identifier}`"))
    assert "provider_failure" in matrix_row
    assert matrix_row.count("| -- ") == 6, "every score column is withheld"
    assert "Numbers withheld" in report
    # The evidence is still linked, so the failure can be inspected.
    assert f"runs/{identifier}/grade.json" in report


def test_an_artifact_this_build_cannot_read_fails_only_its_own_row(tmp_path: Path) -> None:
    """One damaged bundle must not abort the packet for every other row."""
    _run_a_managed_bundle(tmp_path / "runs" / "row-1")
    _run_a_managed_bundle(tmp_path / "runs" / "row-2")
    (tmp_path / "runs" / "row-1" / "grade.json").write_bytes(b"\xff\xfe not utf-8")

    plan = _plan(
        levels=[CASE, "quiet-tuesday-001"],
        bundles={
            row_id(CASE, "stub:vendor/boss"): "runs/row-1",
            row_id("quiet-tuesday-001", "stub:vendor/boss"): "runs/row-2",
        },
    )
    manifest = _package(plan, tmp_path)

    damaged, other = manifest["rows"]
    assert damaged["status"] == "incomplete"
    assert damaged["missingFiles"] == ["grade.json"]
    assert not (
        tmp_path / "packet" / "sanitized" / "runs" / damaged["rowId"] / "grade.json"
    ).exists(), "an artifact that cannot be scanned for credentials does not travel"
    # The second row is a dinner-rush bundle under a quiet-tuesday row, so it is
    # mismatched -- but it was still collected, which is what this pins.
    assert other["status"] == "mismatched"


def test_a_half_built_packet_is_not_merged_into(tmp_path: Path) -> None:
    """A build that died leaves runs/ and no report; a retry must not merge."""
    orphan = tmp_path / "packet" / "sanitized" / "runs" / "left-behind"
    orphan.mkdir(parents=True)
    (orphan / "grade.json").write_text("{}")

    with pytest.raises(PacketExistsError, match="runs/"):
        _package(_plan(), tmp_path)
    _package(_plan(), tmp_path, overwrite=True)
    assert not orphan.exists()
