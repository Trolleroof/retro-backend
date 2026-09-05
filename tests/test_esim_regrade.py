"""Offline re-grading of a trajectory already on disk (#72).

The grader itself is covered by ``test_esim_grader.py``, and those tests build
every trajectory in memory. Nothing there crosses a serialization boundary, so
nothing there can catch a ``Trajectory`` that survives the round trip through
JSON with different contents -- or one that no longer parses at all.

That gap is the whole risk re-grading carries. Re-scoring an archive means
reading bundles this build did not write, and a required field added to
``Trajectory`` makes every one of them ungradeable without a single existing
test going red.
"""

import json
from pathlib import Path

import pytest
from esim_rollout import (  # pyright: ignore[reportImplicitRelativeImport]
    greedy_rollout,
    idle_rollout,
)

from retro_backend.esim.cli import (
    TrajectoryLoadError,
    load_trajectory,
    main,
    penalty_lines,
)
from retro_backend.esim.grader import grade
from retro_backend.esim.trajectory import ActionRecord, Trajectory

ROOT = Path(__file__).resolve().parents[1]
FROZEN = ROOT / "tests/fixtures/trajectories/greedy-dinner-rush-001-seed42.json"
SEEDS = (7, 42, 218220)


def _write(path: Path, trajectory: Trajectory) -> Path:
    path.write_text(json.dumps(trajectory.model_dump(by_alias=True, mode="json"), sort_keys=True))
    return path


def _unsafe_serve() -> ActionRecord:
    """An unverified allergy serve, which the simulator would have refused.

    Spliced in for the same reason ``test_esim_grader.py`` splices it: a real
    ``RestaurantSim`` never produces one, so the hard gate is unreachable
    from a genuine rollout.
    """
    return ActionRecord(
        tick=12,
        role="server",
        actor_id="worker-server",
        tool="serve",
        arguments={"orderId": "order-2", "verified": False},
        request_id="spliced-12-serve",
        accepted=True,
    )


class TestRoundTrip:
    """A grade must not depend on whether the trajectory came from memory."""

    @pytest.mark.parametrize("seed", SEEDS)
    def test_grading_through_disk_matches_grading_in_memory(
        self, seed: int, tmp_path: Path
    ) -> None:
        live = greedy_rollout(seed)
        reloaded = load_trajectory(_write(tmp_path / "trajectory.json", live))
        assert reloaded == live
        assert grade(reloaded) == grade(live)

    def test_an_idle_run_survives_the_round_trip_too(self, tmp_path: Path) -> None:
        live = idle_rollout(42)
        reloaded = load_trajectory(_write(tmp_path / "trajectory.json", live))
        assert grade(reloaded) == grade(live)

    def test_a_spliced_unsafe_run_survives_the_round_trip(self, tmp_path: Path) -> None:
        # The failing case matters more than the passing one: a serialization
        # bug that dropped `accepted` would silently un-fail the safety gate.
        live = greedy_rollout(42).model_copy(
            update={"actions": greedy_rollout(42).actions + (_unsafe_serve(),)}
        )
        reloaded = load_trajectory(_write(tmp_path / "trajectory.json", live))
        assert grade(reloaded).safety_failure is True
        assert grade(reloaded) == grade(live)


class TestSchemaCompatibility:
    """The frozen bundle re-grading promises to keep reading.

    This is a *schema* guard, not a grade pin -- the deterministic generators
    already pin every score in ``TestPinnedBaselines``. What is fixed here is
    the on-disk layout: the oldest trajectory an archive re-grade must still
    be able to open.
    """

    def test_the_frozen_bundle_still_parses(self) -> None:
        assert load_trajectory(FROZEN).case_id == "dinner-rush-001"

    def test_the_frozen_bundle_still_grades(self) -> None:
        # If this fails, every trajectory already written to disk has become
        # ungradeable, and re-scoring the archive is impossible.
        assert grade(load_trajectory(FROZEN)) == grade(greedy_rollout(42))


class TestLoadFailuresAreNamed:
    """A bad bundle must say which file, not raise a bare ValidationError."""

    def test_a_missing_file_names_the_path(self, tmp_path: Path) -> None:
        missing = tmp_path / "nope.json"
        with pytest.raises(TrajectoryLoadError, match="No such file"):
            load_trajectory(missing)

    def test_a_directory_is_reported_not_raised_as_a_traceback(self, tmp_path: Path) -> None:
        # Only FileNotFoundError was caught here once, so a directory (or a
        # permission denial) escaped main() as a traceback and the caller
        # never saw the documented exit code.
        with pytest.raises(TrajectoryLoadError, match="Is a directory"):
            load_trajectory(tmp_path)

    def test_a_directory_exits_four_rather_than_crashing(self, tmp_path: Path) -> None:
        assert main(["regrade", "--trajectory", str(tmp_path)]) == 4

    def test_malformed_json_names_the_path(self, tmp_path: Path) -> None:
        path = tmp_path / "trajectory.json"
        path.write_text("{not json")
        with pytest.raises(TrajectoryLoadError, match="not valid JSON"):
            load_trajectory(path)

    def test_a_payload_that_is_not_a_trajectory_is_rejected(self, tmp_path: Path) -> None:
        path = tmp_path / "trajectory.json"
        path.write_text(json.dumps({"caseId": "dinner-rush-001"}))
        with pytest.raises(TrajectoryLoadError, match="does not match"):
            load_trajectory(path)

    def test_an_extra_field_is_rejected_rather_than_ignored(self, tmp_path: Path) -> None:
        # `Trajectory` forbids extras. A bundle from a *newer* build is not
        # silently degraded into a gradeable one.
        payload = json.loads(FROZEN.read_text())
        payload["somethingNew"] = 1
        path = tmp_path / "trajectory.json"
        path.write_text(json.dumps(payload))
        with pytest.raises(TrajectoryLoadError):
            load_trajectory(path)


class TestPenaltyReporting:
    """Every penalty names the tick and the event behind it."""

    def test_a_safety_violation_names_its_tick_and_tool(self) -> None:
        live = greedy_rollout(42)
        unsafe = live.model_copy(update={"actions": live.actions + (_unsafe_serve(),)})
        lines = penalty_lines(grade(unsafe), unsafe)
        violation = [line for line in lines if "VIOLATION" in line]
        assert len(violation) == 1
        assert "tick=12" in violation[0]
        assert "tool=serve" in violation[0]
        assert "actor=worker-server" in violation[0]

    def test_a_blocked_attempt_is_labelled_blocked_not_violation(self) -> None:
        blocked = ActionRecord(
            tick=12,
            role="server",
            actor_id="worker-server",
            tool="serve",
            arguments={"orderId": "order-2", "verified": False},
            request_id="spliced-12-blocked",
            accepted=False,
            error_code="unsafe_action",
        )
        live = greedy_rollout(42)
        spliced = live.model_copy(update={"actions": live.actions + (blocked,)})
        graded = grade(spliced)
        lines = penalty_lines(graded, spliced)
        assert graded.safety_failure is False
        assert any("blocked" in line and "tick=12" in line for line in lines)
        assert not any("VIOLATION" in line for line in lines)

    def test_incomplete_orders_are_counted_and_named(self) -> None:
        # An idle run serves nothing, so completion is the penalty to explain.
        idle = idle_rollout(42)
        graded = grade(idle)
        assert graded.outcome.order_completion < 1.0
        completion = [line for line in penalty_lines(graded, idle) if "COMPLETION" in line]
        assert len(completion) == 1
        assert "order-" in completion[0]

    def test_a_clean_run_reports_no_penalties(self) -> None:
        clean = greedy_rollout(42)
        graded = grade(clean)
        lines = penalty_lines(graded, clean)
        assert not any("VIOLATION" in line for line in lines)


class TestRegradeCommand:
    """The CLI surface, end to end."""

    def test_regrade_writes_a_grade_matching_the_inline_grader(self, tmp_path: Path) -> None:
        out = tmp_path / "grade.json"
        assert main(["regrade", "--trajectory", str(FROZEN), "--output", str(out)]) == 0
        written = json.loads(out.read_text())
        expected = grade(greedy_rollout(42)).model_dump(by_alias=True, mode="json")
        assert written == expected

    def test_regrade_stamps_the_grader_version(self, tmp_path: Path) -> None:
        out = tmp_path / "grade.json"
        main(["regrade", "--trajectory", str(FROZEN), "--output", str(out)])
        assert json.loads(out.read_text())["graderVersion"] == "restaurant-esim-grader/v1"

    def test_regrade_without_output_writes_no_file(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        assert main(["regrade", "--trajectory", str(FROZEN)]) == 0
        assert list(tmp_path.iterdir()) == []
        assert "dinner-rush-001" in capsys.readouterr().out

    def test_an_unreadable_trajectory_exits_four(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        path = tmp_path / "trajectory.json"
        path.write_text("{not json")
        assert main(["regrade", "--trajectory", str(path)]) == 4
        assert "unreadable trajectory" in capsys.readouterr().err

    def test_the_printed_report_names_the_offending_tick(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        live = greedy_rollout(42)
        unsafe = live.model_copy(update={"actions": live.actions + (_unsafe_serve(),)})
        path = _write(tmp_path / "trajectory.json", unsafe)
        assert main(["regrade", "--trajectory", str(path)]) == 0
        out = capsys.readouterr().out
        assert "safetyFailure=True" in out
        assert "tick=12" in out
