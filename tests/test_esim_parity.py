"""Cross-engine parity ingest (#72), corrected against the Tui side's answers.

Trolleroof/tui-gamepigeon#277 (comment 5554247493) answered three questions
this module had raised, and answering them exposed a real bug in the
original design: the "encoding" check hashed the export's raw JSON against
that same JSON re-serialized through our own model. Those always differed --
Pydantic coerces an int into a float on a float-typed field and fills an
absent key with ``None`` -- regardless of whether the underlying restaurant
state matched at all. It was never a report of two engines disagreeing.

The corrected design compares the export's ``initialState`` against genuine
ground truth: this engine's own ``sim.reset(seed)`` for the case the trace
names. Wire-format spelling (``70`` vs ``70.0``) is irrelevant here -- both
sides are parsed through the same Pydantic model before anything is hashed.

``TUI_LIVE`` is the one real export available, fetched from
docs/evidence/issue-275/played-shift.json at commit a9220e2. It is a
``live`` run and carries no ``caseId``, so it cannot be checked against
ground truth or replayed -- see Trolleroof/retro-backend#78.
"""

import json
from pathlib import Path
from typing import Any

import pytest

from retro_backend.esim.cli import main
from retro_backend.esim.parity import TraceLoadError, check_parity, load_trace

ROOT = Path(__file__).resolve().parents[1]
TRACES = ROOT / "tests/fixtures/traces"
NATIVE = TRACES / "python-native-trace-v1.json"
JS_STYLE = TRACES / "js-style-trace-v1.json"
TUI_LIVE = TRACES / "tui-live-trace-v3.json"


def _native() -> dict[str, Any]:
    """The export as raw JSON. Typed loosely because these tests mutate it."""
    return json.loads(NATIVE.read_text())


class TestV3Support:
    """v3 (Trolleroof/tui-gamepigeon#276) added event kinds and fields on
    top of v1 -- nothing removed -- so a real export in that format must
    load, not be refused as an unrecognized version.
    """

    def test_a_real_tui_v3_export_loads(self) -> None:
        payload = json.loads(TUI_LIVE.read_text())
        trace = load_trace(payload)
        assert trace.version == "restaurant-arena-trace/v3"
        assert trace.case_id is None  # v3 carries no caseId; see #278

    def test_its_inspection_kind_events_are_not_rejected(self) -> None:
        trace = load_trace(json.loads(TUI_LIVE.read_text()))
        kinds = {event.kind for event in trace.events}
        assert "inspection" in kinds

    def test_a_real_export_with_no_case_id_is_unproven_not_crashed(self) -> None:
        # Before the ground-truth redesign this reported "mismatch" on
        # spelling artifacts that were never a real disagreement. Now that
        # envVersion is not gated on and initialState is checked against a
        # named case, a live export with neither simply cannot be verified.
        report = check_parity(json.loads(TUI_LIVE.read_text()))
        assert report.verdict == "unproven"
        assert "no caseId" in report.reason


class TestUnreadableExports:
    """A refusal is a result. Nothing is reshaped to make it parse."""

    def test_an_unknown_role_is_refused_not_mapped(self) -> None:
        payload = _native()
        events = list(payload["events"])
        events[0] = {**events[0], "role": "sommelier"}
        payload["events"] = events
        with pytest.raises(TraceLoadError, match="not a restaurant-arena-trace"):
            load_trace(payload)

    def test_an_unmodelled_field_is_refused_rather_than_ignored(self) -> None:
        payload = _native()
        payload["somethingNew"] = 1
        with pytest.raises(TraceLoadError):
            load_trace(payload)

    def test_a_malformed_state_hash_is_refused(self) -> None:
        payload = _native()
        events = list(payload["events"])
        events[0] = {**events[0], "stateHash": "md5:whatever"}
        payload["events"] = events
        with pytest.raises(TraceLoadError):
            load_trace(payload)


class TestContractGate:
    def test_a_version_mismatch_stops_before_anything_else(self) -> None:
        payload = _native()
        payload["contractVersion"] = "restaurant-arena/v2"
        report = check_parity(payload)
        assert report.verdict == "mismatch"
        assert report.differences == ()
        assert "nothing was compared" in report.reason

    def test_env_version_is_not_gated_on(self) -> None:
        # Confirmed by the Tui side: envVersion is the producer's own build
        # version (kept in step with package.json), not a simulator
        # revision, and must never be compared with this engine's own.
        payload = _native()
        payload["envVersion"] = "0.3.4"
        report = check_parity(payload)
        assert report.verdict != "mismatch" or "envVersion" not in report.reason


class TestNoCaseId:
    """No caseId means no ground truth for initialState and no schedule."""

    def test_reports_unproven_not_a_spelling_mismatch(self) -> None:
        payload = _native()
        del payload["caseId"]
        report = check_parity(payload)
        assert report.verdict == "unproven"
        assert report.differences == ()

    def test_the_reason_names_both_missing_capabilities(self) -> None:
        payload = _native()
        del payload["caseId"]
        reason = check_parity(payload).reason
        assert "ground truth" in reason
        assert "event schedule" in reason


class TestGroundTruthComparison:
    """initialState is checked against this engine's own sim.reset(seed)."""

    def test_a_faithful_export_matches_ground_truth(self) -> None:
        report = check_parity(_native())
        assert report.verdict == "unproven"  # matched, but see TestReplay
        assert "initial state matched" in report.reason

    def test_wire_spelling_of_integral_floats_does_not_matter(self) -> None:
        # The original bug this test guards against: hashing raw exported
        # JSON against a Pydantic-coerced re-dump always disagreed on every
        # integral float, independent of whether the state actually matched.
        # Both engines parse through the same model now, so it is a no-op.
        report = check_parity(json.loads(JS_STYLE.read_text()))
        assert report.verdict == "unproven"
        assert "initial state matched" in report.reason

    def test_a_genuine_content_difference_is_still_caught(self) -> None:
        payload = _native()
        state = dict(payload["initialState"])
        state["reputation"] = 12
        payload["initialState"] = state
        report = check_parity(payload)
        assert report.verdict == "mismatch"
        assert any("/reputation" in d for d in report.differences)

    def test_an_unknown_case_is_reported_not_guessed(self) -> None:
        payload = _native()
        payload["caseId"] = "no-such-case"
        report = check_parity(payload)
        assert report.verdict == "mismatch"
        assert "no-such-case" in report.reason


class TestReplay:
    """Actions replay against the reset state, but nothing verifies the
    result: the export carries no hash comparable with RestaurantState.
    """

    def test_a_faithful_export_replays_without_error(self) -> None:
        report = check_parity(_native())
        assert report.verdict == "unproven"
        assert "replayed 50 action(s)" in report.reason

    def test_replay_never_returns_parity(self) -> None:
        # Exit 0 is reserved for a state-only checkpoint hash Tui does not
        # expose yet (see #277). Nothing may reach it by comparing against
        # events[].stateHash or replay.inputs[].stateHash -- confirmed by
        # the Tui side to be different hash families entirely.
        assert check_parity(_native()).verdict != "parity"

    def test_an_unreadable_action_is_raised_not_skipped(self) -> None:
        # Skipping it would replay a shorter episode than the one exported
        # and then blame the resulting divergence on the engine.
        payload = _native()
        for event in payload["events"]:
            if event["kind"] == "action":
                event["action"] = {"nonsense": True}
                break
        with pytest.raises(TraceLoadError, match="cannot read"):
            check_parity(payload)


class TestParityCommand:
    def test_an_incomparable_export_exits_five(self, tmp_path: Path) -> None:
        payload = _native()
        payload["contractVersion"] = "restaurant-arena/v2"
        path = tmp_path / "trace.json"
        path.write_text(json.dumps(payload))
        assert main(["parity", "--trace", str(path)]) == 5

    def test_an_unproven_export_exits_six_not_zero(self) -> None:
        assert main(["parity", "--trace", str(NATIVE)]) == 6

    def test_an_unreadable_export_exits_four(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        path = tmp_path / "trace.json"
        path.write_text("{not json")
        assert main(["parity", "--trace", str(path)]) == 4
        assert "unreadable trace" in capsys.readouterr().err

    def test_a_missing_export_exits_four(self, tmp_path: Path) -> None:
        assert main(["parity", "--trace", str(tmp_path / "nope.json")]) == 4

    def test_a_directory_exits_four_rather_than_crashing(self, tmp_path: Path) -> None:
        assert main(["parity", "--trace", str(tmp_path)]) == 4

    def test_the_report_can_be_written_as_json(self, tmp_path: Path) -> None:
        out = tmp_path / "parity.json"
        main(["parity", "--trace", str(NATIVE), "--output", str(out)])
        written = json.loads(out.read_text())
        assert written["verdict"] == "unproven"
