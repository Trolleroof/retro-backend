"""Cross-engine parity ingest (#72).

The export these tests exercise is synthetic. The real D2-A Tui session was
not available when this was written, so nothing here should be read as
evidence that the Tui client and this engine agree -- only as evidence that
the check reports honestly on the exports it is given.

The JS-style fixture is the interesting one. It is the same episode as the
Python-native fixture with every integral float written the way
``JSON.stringify`` writes it, which is the divergence ``state_hash`` has
documented since it was written. It exists so the detector is proven against
the failure that is actually expected, rather than only against a passing case.
"""

import json
from pathlib import Path
from typing import Any

import pytest

from retro_backend.esim.case import ENV_VERSION
from retro_backend.esim.cli import main
from retro_backend.esim.contracts import STATE_VERSION
from retro_backend.esim.parity import (
    TraceLoadError,
    check_parity,
    load_trace,
    numerically_equal,
)

ROOT = Path(__file__).resolve().parents[1]
TRACES = ROOT / "tests/fixtures/traces"
NATIVE = TRACES / "python-native-trace-v1.json"
JS_STYLE = TRACES / "js-style-trace-v1.json"


def _native() -> dict[str, Any]:
    """The export as raw JSON. Typed loosely because these tests mutate it."""
    return json.loads(NATIVE.read_text())


class TestReadableExports:
    def test_a_well_formed_export_parses(self) -> None:
        trace = load_trace(_native())
        assert trace.episode_id == "episode-parity-demo"
        assert trace.contract_version == STATE_VERSION

    def test_an_unknown_role_is_refused_not_mapped(self) -> None:
        # The whole point of the card: no role coercion. A foreign engine
        # with a "sommelier" is a compatibility failure, not a chef.
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
    """A version mismatch stops before anything is compared."""

    def test_a_foreign_contract_version_blocks_the_check(self) -> None:
        payload = _native()
        payload["contractVersion"] = "restaurant-arena/v2"
        report = check_parity(payload)
        assert report.verdict == "contract_mismatch"
        assert report.comparable is False
        assert report.encoding is None
        assert "restaurant-arena/v2" in (report.blocked_reason or "")

    def test_a_foreign_env_version_blocks_the_check(self) -> None:
        payload = _native()
        payload["envVersion"] = "restaurant-e-sim/v9"
        report = check_parity(payload)
        assert report.verdict == "contract_mismatch"
        assert report.encoding is None

    def test_the_report_names_both_sides_of_the_mismatch(self) -> None:
        payload = _native()
        payload["contractVersion"] = "restaurant-arena/v2"
        report = check_parity(payload)
        assert report.trace_contract_version == "restaurant-arena/v2"
        assert report.engine_contract_version == STATE_VERSION
        assert report.engine_env_version == ENV_VERSION


class TestEncodingDivergence:
    """The documented Python/JavaScript number-spelling gap."""

    def test_a_js_style_export_is_caught(self) -> None:
        report = check_parity(json.loads(JS_STYLE.read_text()))
        assert report.verdict == "encoding_divergence"
        assert report.comparable is False

    def test_the_divergence_is_classified_as_spelling_not_content(self) -> None:
        # This is the distinction that makes the diagnostic actionable. The
        # two engines hold the same restaurant; only the serializer differs.
        report = check_parity(json.loads(JS_STYLE.read_text()))
        assert report.encoding is not None
        assert report.encoding.divergences
        assert all(d.numeric_spelling_only for d in report.encoding.divergences)

    def test_a_divergence_names_the_field_and_both_spellings(self) -> None:
        report = check_parity(json.loads(JS_STYLE.read_text()))
        assert report.encoding is not None
        by_path = {d.path: d for d in report.encoding.divergences}
        reading = by_path["/equipment[1]/reading"]
        assert reading.exported == "62"
        assert reading.normalized == "62.0"

    def test_no_tick_comparison_is_reported_when_hashes_are_incomparable(self) -> None:
        report = check_parity(json.loads(JS_STYLE.read_text()))
        assert report.replay_attempted is False
        assert "none was run" in (report.blocked_reason or "")

    def test_a_field_the_export_omits_is_reported_as_content(self) -> None:
        # An export that leaves out a field our models default is a genuine
        # difference, not a spelling one: no serializer change reconciles it,
        # and the reader must not be told to go looking for one. Changing a
        # *value* would not test this -- both sides move together.
        payload = _native()
        state = dict(payload["initialState"])
        del state["version"]
        payload["initialState"] = state
        report = check_parity(payload)
        assert report.verdict == "encoding_divergence"
        assert report.encoding is not None
        content = [d for d in report.encoding.divergences if not d.numeric_spelling_only]
        assert [d.path for d in content] == ["/version"]
        assert content[0].exported == "None"
        assert content[0].normalized == "'restaurant-arena/v1'"

    def test_a_boolean_is_never_reported_as_a_number_spelling(self) -> None:
        # Python makes True == 1. Reporting that as a serializer difference
        # would send a reader hunting a bug that is not there.
        assert numerically_equal(True, 1) is False
        assert numerically_equal(1, 1.0) is True


class TestParityIsNeverClaimed:
    """The check must not report a pass it did not earn."""

    def test_a_clean_export_is_unproven_not_proven(self) -> None:
        report = check_parity(_native())
        assert report.verdict == "parity_unproven"
        assert report.replay_attempted is False

    def test_the_reason_names_the_missing_event_schedule(self) -> None:
        report = check_parity(_native())
        assert "event schedule" in (report.blocked_reason or "")

    def test_hashes_agree_on_an_export_this_engine_wrote(self) -> None:
        report = check_parity(_native())
        assert report.encoding is not None
        assert report.encoding.agrees is True
        assert report.encoding.exported_hash == report.encoding.normalized_hash


class TestParityCommand:
    def test_an_incomparable_export_exits_five(self) -> None:
        assert main(["parity", "--trace", str(JS_STYLE)]) == 5

    def test_an_unproven_export_exits_six_not_zero(self) -> None:
        # Exit 0 would read as "parity passed" to any caller that checks
        # only the status. Nothing may reach it until replay runs.
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
        assert written["verdict"] == "encoding_divergence"
        assert written["encoding"]["divergences"]

    def test_the_printed_report_separates_spelling_from_content(
        self, capsys: pytest.CaptureFixture[str]
    ) -> None:
        main(["parity", "--trace", str(JS_STYLE)])
        out = capsys.readouterr().out
        assert "verdict=encoding_divergence" in out
        assert "SPELLING /equipment[1]/reading" in out
        assert "CONTENT" not in out
