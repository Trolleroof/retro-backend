"""Cross-engine parity ingest (#72).

The exports here are synthetic. The real D2-A Tui session was not available
when this was written, so nothing here is evidence that the Tui client and
this engine agree -- only that the check reports honestly on what it is given.

The JS-style fixture is the same episode with integral floats written the way
``JSON.stringify`` writes them. It exists so the check is exercised against
the failure that is actually expected, not only against a passing case.
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


def _native() -> dict[str, Any]:
    """The export as raw JSON. Typed loosely because these tests mutate it."""
    return json.loads(NATIVE.read_text())


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


class TestMismatch:
    def test_a_version_mismatch_stops_before_hashing(self) -> None:
        payload = _native()
        payload["contractVersion"] = "restaurant-arena/v2"
        report = check_parity(payload)
        assert report.verdict == "mismatch"
        assert report.differences == ()
        assert "nothing was compared" in report.reason

    def test_a_js_style_export_is_caught(self) -> None:
        report = check_parity(json.loads(JS_STYLE.read_text()))
        assert report.verdict == "mismatch"
        assert "70.0, not 70" in report.reason

    def test_the_differing_fields_are_named_with_both_spellings(self) -> None:
        report = check_parity(json.loads(JS_STYLE.read_text()))
        assert "/equipment[1]/reading: exported=62 engine=62.0" in report.differences

    def test_the_field_list_is_capped(self) -> None:
        # 13 fields differ; an uncapped list buries the useful ones.
        report = check_parity(json.loads(JS_STYLE.read_text()))
        assert len(report.differences) == 11
        assert report.differences[-1] == "... and 3 more field(s)"

    def test_a_field_the_export_omits_is_reported_too(self) -> None:
        # Changing a *value* would not test this -- both sides move together.
        payload = _native()
        state = dict(payload["initialState"])
        del state["version"]
        payload["initialState"] = state
        report = check_parity(payload)
        assert report.verdict == "mismatch"
        assert report.differences == ("/version: exported=None engine='restaurant-arena/v1'",)


class TestParityIsNeverClaimed:
    def test_a_clean_export_is_unproven_not_proven(self) -> None:
        report = check_parity(_native())
        assert report.verdict == "unproven"

    def test_the_reason_names_the_missing_event_schedule(self) -> None:
        assert "event schedule" in check_parity(_native()).reason


class TestParityCommand:
    def test_an_incomparable_export_exits_five(self) -> None:
        assert main(["parity", "--trace", str(JS_STYLE)]) == 5

    def test_an_unproven_export_exits_six_not_zero(self) -> None:
        # Exit 0 would read as "parity passed" to a caller checking only the
        # status. Nothing may reach it until replay runs.
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

    def test_the_report_can_be_written_as_json(self, tmp_path: Path) -> None:
        out = tmp_path / "parity.json"
        main(["parity", "--trace", str(JS_STYLE), "--output", str(out)])
        written = json.loads(out.read_text())
        assert written["verdict"] == "mismatch"
        assert written["differences"]
