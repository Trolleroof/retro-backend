"""Drive scripts/download-r2-dataset.py against a stubbed wrangler.

The script only reaches D1 and R2 through ``npx --yes wrangler@<pin> …``, so a
fake ``npx`` on PATH is enough to exercise every branch without a network. The
fake serves the D1 catalog from ``catalog.json`` and R2 objects from an
``objects/`` tree, both under the directory named by ``FAKE_R2_ROOT``.
"""

import hashlib
import json
import os
import re
import subprocess
import sys
from pathlib import Path
from typing import Any

import pytest

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "scripts" / "download-r2-dataset.py"
MAKEFILE = ROOT / "Makefile"
PREFIX = "episodes/parquet/v1"

FAKE_WRANGLER = '''
import json
import shutil
import sys
from pathlib import Path

root = Path(sys.argv[1])
args = sys.argv[2:]
# Drop the `--yes wrangler@<version>` prelude that the script always passes.
assert args[0] == "--yes", args
assert args[1].startswith("wrangler@"), args
args = args[2:]

if args[:2] == ["d1", "execute"]:
    catalog = json.loads((root / "catalog.json").read_text())
    print("Executing on local database …")
    print(json.dumps([{"results": catalog, "success": True}]))
    raise SystemExit(0)

if args[:3] == ["r2", "object", "get"]:
    source = root / "objects" / args[3].split("/", 1)[1]
    destination = Path(args[args.index("--file") + 1])
    if not source.is_file():
        print(f"The specified key does not exist: {args[3]}", file=sys.stderr)
        raise SystemExit(1)
    shutil.copyfile(source, destination)
    raise SystemExit(0)

print(f"unexpected wrangler invocation: {args}", file=sys.stderr)
raise SystemExit(2)
'''


def _install_fake_wrangler(tmp_path: Path) -> tuple[Path, dict[str, str]]:
    """Put a fake ``npx`` on PATH and return the fake R2 root plus the env."""
    fake_root = tmp_path / "r2"
    (fake_root / "objects").mkdir(parents=True)
    (fake_root / "catalog.json").write_text("[]", encoding="utf-8")

    handler = tmp_path / "fake_wrangler.py"
    handler.write_text(FAKE_WRANGLER, encoding="utf-8")

    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    npx = bin_dir / "npx"
    npx.write_text(
        f'#!/bin/sh\nexec {sys.executable} {handler} {fake_root} "$@"\n',
        encoding="utf-8",
    )
    npx.chmod(0o755)

    environment = os.environ | {"PATH": f"{bin_dir}:{os.environ['PATH']}"}
    return fake_root, environment


def _seed_episode(
    fake_root: Path,
    *,
    game_type: str,
    game_id: str,
    day: str,
    body: bytes = b"PAR1-fake-parquet",
    manifest_sha256: str | None = None,
    with_manifest: bool = True,
    catalog_sha256: str | None = None,
) -> dict[str, Any]:
    """Write one Parquet object (+ manifest) into the fake bucket and catalog it."""
    year, month, date_day = day.split("-")
    key = f"{PREFIX}/{game_type}/{year}/{month}/{date_day}/{game_id}.parquet"
    manifest_key = f"{PREFIX}/{game_type}/{year}/{month}/{date_day}/{game_id}.manifest.json"
    digest = hashlib.sha256(body).hexdigest()

    parquet_path = fake_root / "objects" / key
    parquet_path.parent.mkdir(parents=True, exist_ok=True)
    _ = parquet_path.write_bytes(body)
    if with_manifest:
        _ = (fake_root / "objects" / manifest_key).write_text(
            json.dumps(
                {
                    "version": 1,
                    "game_id": game_id,
                    "game_type": game_type,
                    "status": "complete",
                    "row_count": 3,
                    "parquet_key": key,
                    "parquet_bytes": len(body),
                    "parquet_sha256": manifest_sha256 or digest,
                }
            ),
            encoding="utf-8",
        )

    row: dict[str, Any] = {
        "game_type": game_type,
        "game_id": game_id,
        "completed_at_ms": 1760000000000,
        "parquet_key": key,
        "manifest_key": manifest_key,
        "parquet_bytes": len(body),
        "parquet_sha256": catalog_sha256 if catalog_sha256 is not None else digest,
    }
    catalog: list[dict[str, Any]] = json.loads((fake_root / "catalog.json").read_text())
    catalog.append(row)
    _ = (fake_root / "catalog.json").write_text(json.dumps(catalog), encoding="utf-8")
    return row


def _run(environment: dict[str, str], *args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, str(SCRIPT), *args],
        check=False,
        capture_output=True,
        text=True,
        env=environment,
    )


def _tree(output_dir: Path) -> list[str]:
    return sorted(
        str(path.relative_to(output_dir))
        for path in output_dir.rglob("*")
        if path.is_file()
    )


def test_wrangler_pin_matches_the_makefile() -> None:
    makefile = re.search(r"^WRANGLER_VERSION \?= (\S+)$", MAKEFILE.read_text(), re.MULTILINE)
    script = re.search(r'^WRANGLER_VERSION = "(\S+)"$', SCRIPT.read_text(), re.MULTILINE)
    assert makefile is not None and script is not None
    assert script.group(1) == makefile.group(1)


def test_downloads_only_the_requested_type_and_date_window(tmp_path: Path) -> None:
    fake_root, environment = _install_fake_wrangler(tmp_path)
    _ = _seed_episode(fake_root, game_type="2048", game_id="game_a", day="2026-07-01")
    _ = _seed_episode(fake_root, game_type="2048", game_id="game_b", day="2026-07-15")
    _ = _seed_episode(fake_root, game_type="2048", game_id="game_c", day="2026-08-02")
    _ = _seed_episode(fake_root, game_type="snake", game_id="game_d", day="2026-07-15")
    output = tmp_path / "out"

    result = _run(
        environment,
        "--game-type", "2048",
        "--output-dir", str(output),
        "--since", "2026-07-10",
        "--until", "2026-07-31",
    )

    assert result.returncode == 0, result.stdout + result.stderr
    assert _tree(output) == [
        "2048/2026-07-15/game_b.manifest.json",
        "2048/2026-07-15/game_b.parquet",
    ]
    assert "episodes downloaded: 1" in result.stdout
    assert "episodes failed: 0" in result.stdout
    assert f"total bytes downloaded: {len(b'PAR1-fake-parquet')}" in result.stdout


def test_all_game_types_with_limit(tmp_path: Path) -> None:
    fake_root, environment = _install_fake_wrangler(tmp_path)
    _ = _seed_episode(fake_root, game_type="2048", game_id="game_a", day="2026-07-01")
    _ = _seed_episode(fake_root, game_type="snake", game_id="game_b", day="2026-07-02")
    _ = _seed_episode(fake_root, game_type="tetris", game_id="game_c", day="2026-07-03")
    output = tmp_path / "out"

    result = _run(
        environment,
        "--game-type", "all",
        "--output-dir", str(output),
        "--limit", "2",
    )

    assert result.returncode == 0, result.stdout + result.stderr
    assert _tree(output) == [
        "2048/2026-07-01/game_a.manifest.json",
        "2048/2026-07-01/game_a.parquet",
        "snake/2026-07-02/game_b.manifest.json",
        "snake/2026-07-02/game_b.parquet",
    ]
    assert "episodes downloaded: 2" in result.stdout


def test_no_manifests_keeps_only_parquet(tmp_path: Path) -> None:
    fake_root, environment = _install_fake_wrangler(tmp_path)
    _ = _seed_episode(fake_root, game_type="2048", game_id="game_a", day="2026-07-01")
    output = tmp_path / "out"

    result = _run(
        environment,
        "--game-type", "2048",
        "--output-dir", str(output),
        "--no-manifests",
    )

    assert result.returncode == 0, result.stdout + result.stderr
    assert _tree(output) == ["2048/2026-07-01/game_a.parquet"]


def test_checksum_mismatch_is_reported_and_the_file_is_discarded(tmp_path: Path) -> None:
    fake_root, environment = _install_fake_wrangler(tmp_path)
    _ = _seed_episode(
        fake_root,
        game_type="2048",
        game_id="game_bad",
        day="2026-07-01",
        manifest_sha256="0" * 64,
    )
    _ = _seed_episode(fake_root, game_type="2048", game_id="game_ok", day="2026-07-01")
    output = tmp_path / "out"

    result = _run(environment, "--game-type", "2048", "--output-dir", str(output))

    assert result.returncode == 1, result.stdout + result.stderr
    assert "checksum mismatch, discarded" in result.stdout
    assert "episodes failed: 1" in result.stdout
    assert "episodes downloaded: 1" in result.stdout
    # The good episode still lands; the corrupt one leaves nothing behind.
    assert _tree(output) == [
        "2048/2026-07-01/game_ok.manifest.json",
        "2048/2026-07-01/game_ok.parquet",
    ]


def test_missing_manifest_falls_back_to_the_catalog_checksum(tmp_path: Path) -> None:
    fake_root, environment = _install_fake_wrangler(tmp_path)
    _ = _seed_episode(
        fake_root,
        game_type="2048",
        game_id="game_a",
        day="2026-07-01",
        with_manifest=False,
    )
    output = tmp_path / "out"

    result = _run(environment, "--game-type", "2048", "--output-dir", str(output))

    assert result.returncode == 0, result.stdout + result.stderr
    assert "falling back to the catalog checksum" in result.stdout
    assert _tree(output) == ["2048/2026-07-01/game_a.parquet"]


def test_missing_manifest_and_bad_catalog_checksum_fails_the_episode(tmp_path: Path) -> None:
    fake_root, environment = _install_fake_wrangler(tmp_path)
    _ = _seed_episode(
        fake_root,
        game_type="2048",
        game_id="game_a",
        day="2026-07-01",
        with_manifest=False,
        catalog_sha256="not-a-digest",
    )
    output = tmp_path / "out"

    result = _run(environment, "--game-type", "2048", "--output-dir", str(output))

    assert result.returncode == 1, result.stdout + result.stderr
    assert "no usable manifest and no catalog checksum" in result.stdout
    assert _tree(output) == []


def test_second_run_skips_matching_files_and_repairs_corrupt_ones(tmp_path: Path) -> None:
    fake_root, environment = _install_fake_wrangler(tmp_path)
    _ = _seed_episode(fake_root, game_type="2048", game_id="game_a", day="2026-07-01")
    _ = _seed_episode(fake_root, game_type="2048", game_id="game_b", day="2026-07-01")
    output = tmp_path / "out"

    first = _run(environment, "--game-type", "2048", "--output-dir", str(output))
    assert first.returncode == 0, first.stdout + first.stderr
    assert "episodes downloaded: 2" in first.stdout

    # Corrupt one of the two local copies; the resume run must notice and refetch.
    _ = (output / "2048/2026-07-01/game_b.parquet").write_bytes(b"truncated")

    second = _run(environment, "--game-type", "2048", "--output-dir", str(output))

    assert second.returncode == 0, second.stdout + second.stderr
    assert "game_a: already present with a matching checksum" in second.stdout
    assert "game_b: local copy does not match" in second.stdout
    assert "episodes skipped: 1" in second.stdout
    assert "episodes downloaded: 1" in second.stdout
    assert (output / "2048/2026-07-01/game_b.parquet").read_bytes() == b"PAR1-fake-parquet"


def test_dry_run_writes_nothing(tmp_path: Path) -> None:
    fake_root, environment = _install_fake_wrangler(tmp_path)
    _ = _seed_episode(fake_root, game_type="2048", game_id="game_a", day="2026-07-01")
    _ = _seed_episode(fake_root, game_type="snake", game_id="game_b", day="2026-07-02")
    output = tmp_path / "out"

    result = _run(
        environment,
        "--game-type", "all",
        "--output-dir", str(output),
        "--dry-run",
    )

    assert result.returncode == 0, result.stdout + result.stderr
    assert "would download" in result.stdout
    assert "episodes to download: 2" in result.stdout
    assert str(output / "2048/2026-07-01/game_a.parquet") in result.stdout
    assert not output.exists()


def test_dry_run_reports_already_downloaded_episodes_as_skipped(tmp_path: Path) -> None:
    fake_root, environment = _install_fake_wrangler(tmp_path)
    _ = _seed_episode(fake_root, game_type="2048", game_id="game_a", day="2026-07-01")
    output = tmp_path / "out"

    assert _run(environment, "--game-type", "2048", "--output-dir", str(output)).returncode == 0
    result = _run(
        environment,
        "--game-type", "2048",
        "--output-dir", str(output),
        "--dry-run",
    )

    assert result.returncode == 0, result.stdout + result.stderr
    assert "would skip, already present" in result.stdout
    assert "episodes already present: 1" in result.stdout


def test_catalog_rows_outside_the_expected_layout_are_reported(tmp_path: Path) -> None:
    fake_root, environment = _install_fake_wrangler(tmp_path)
    _ = (fake_root / "catalog.json").write_text(
        json.dumps(
            [
                {"game_id": "game_a", "parquet_key": "somewhere/else/game_a.parquet"},
                {"game_id": "game_b", "parquet_key": ""},
            ]
        ),
        encoding="utf-8",
    )
    output = tmp_path / "out"

    result = _run(environment, "--game-type", "all", "--output-dir", str(output))

    assert result.returncode == 1, result.stdout + result.stderr
    assert "does not match the expected layout" in result.stdout
    assert "row has no parquet_key" in result.stdout
    assert "episodes failed: 2" in result.stdout


def test_traversal_in_a_catalog_key_is_rejected(tmp_path: Path) -> None:
    fake_root, environment = _install_fake_wrangler(tmp_path)
    _ = (fake_root / "catalog.json").write_text(
        json.dumps(
            [{"game_id": "x", "parquet_key": f"{PREFIX}/../2026/07/01/game_a.parquet"}]
        ),
        encoding="utf-8",
    )
    output = tmp_path / "out"

    result = _run(environment, "--game-type", "all", "--output-dir", str(output))

    assert result.returncode == 1, result.stdout + result.stderr
    assert "unusable path component" in result.stdout


@pytest.mark.parametrize(
    ("arguments", "message"),
    [
        (["--output-dir", "out"], "--game-type"),
        (["--game-type", "2048"], "--output-dir"),
        (
            ["--game-type", "all", "--game-type", "2048", "--output-dir", "out"],
            "cannot be combined with specific game types",
        ),
        (
            ["--game-type", "2048", "--output-dir", "out", "--since", "07/01/2026"],
            "expected a UTC date as YYYY-MM-DD",
        ),
        (
            ["--game-type", "2048", "--output-dir", "out", "--until", "2026-13-40"],
            "expected a UTC date as YYYY-MM-DD",
        ),
        (
            ["--game-type", "2048", "--output-dir", "out", "--limit", "0"],
            "expected a positive integer",
        ),
        (
            ["--game-type", "2048", "--output-dir", "out", "--limit", "many"],
            "expected an integer",
        ),
        (
            [
                "--game-type", "2048", "--output-dir", "out",
                "--since", "2026-08-01", "--until", "2026-07-01",
            ],
            "--since 2026-08-01 is after --until 2026-07-01",
        ),
        (
            ["--game-type", "2048", "--output-dir", "out", "--manifests", "--no-manifests"],
            "not allowed with argument",
        ),
    ],
)
def test_argument_validation(tmp_path: Path, arguments: list[str], message: str) -> None:
    _, environment = _install_fake_wrangler(tmp_path)

    result = _run(environment, *arguments)

    assert result.returncode == 2, result.stdout + result.stderr
    assert message in result.stderr
