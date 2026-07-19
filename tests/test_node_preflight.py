import os
import shlex
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "scripts" / "node_preflight.py"


def _run_preflight(tmp_path: Path, node_output: str | None) -> subprocess.CompletedProcess[str]:
    if node_output is not None:
        node = tmp_path / "node"
        node.write_text(
            f"#!/bin/sh\nprintf '%s\\n' {shlex.quote(node_output)}\n",
            encoding="utf-8",
        )
        node.chmod(0o755)

    return subprocess.run(
        [sys.executable, str(SCRIPT)],
        check=False,
        capture_output=True,
        text=True,
        env=os.environ | {"PATH": str(tmp_path)},
    )


def test_preflight_accepts_node_22(tmp_path: Path) -> None:
    result = _run_preflight(tmp_path, "v22.23.1")

    assert result.returncode == 0
    assert result.stdout == "Node.js v22.23.1 is supported.\n"
    assert result.stderr == ""


@pytest.mark.parametrize("output", ["v21.7.3", "v23.11.1", "v26.3.0"])
def test_preflight_rejects_other_majors(tmp_path: Path, output: str) -> None:
    result = _run_preflight(tmp_path, output)

    assert result.returncode == 1
    assert f"Node.js 22.x is required (pinned to 22.23.1); found {output}." in result.stderr


@pytest.mark.parametrize("output", ["", "22.23.1", "v22", "v22.23", "v22.23.1 extra"])
def test_preflight_rejects_malformed_output(tmp_path: Path, output: str) -> None:
    result = _run_preflight(tmp_path, output)

    assert result.returncode == 1
    assert "Could not parse `node --version` output" in result.stderr


def test_preflight_reports_missing_node(tmp_path: Path) -> None:
    result = _run_preflight(tmp_path, None)

    assert result.returncode == 1
    assert "`node` was not found on PATH" in result.stderr
    assert "Install Node.js 22.23.1 (see .node-version)." in result.stderr
