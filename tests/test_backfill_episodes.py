"""Drive scripts/backfill-episodes.py against a stubbed R2 listing and wrangler.

The script reaches R2 and D1 two ways: the object inventory comes from the
Cloudflare REST API, and every individual get/put plus the D1 upsert goes
through ``npx --yes wrangler@<pin> …``. A throwaway HTTP server covers the
first and a fake ``npx`` on PATH covers the second, so every branch runs without
touching the network or the account.

The raw chunk bodies are the same v2 envelopes ``tests/episode_fixtures.py``
builds for the live consumer, so the rows this script produces are the rows the
deployed pipeline produces.
"""

import http.server
import json
import os
import re
import subprocess
import sys
import threading
from collections.abc import Iterator
from pathlib import Path
from typing import Any, ClassVar, override
from urllib.parse import parse_qs

import pytest
from episode_fixtures import (  # pyright: ignore[reportImplicitRelativeImport]
    GAME_ID,
    GAME_TYPE,
    USER_ID,
    build_events,
    split_into_chunks,
)

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "scripts" / "backfill-episodes.py"
MAKEFILE = ROOT / "Makefile"
ACCOUNT_ID = "acct-test"
API_TOKEN = "token-test"
BUCKET = "retro-gameplay-raw"
UPLOADED_AT = "2026-07-26T10:00:00.000Z"

FAKE_WRANGLER = '''
import json
import shutil
import sys
from pathlib import Path

root = Path(sys.argv[1])
args = sys.argv[2:]
assert args[0] == "--yes", args
assert args[1].startswith("wrangler@"), args
args = args[2:]

log = root / "calls.log"
with log.open("a") as handle:
    handle.write(json.dumps(args) + "\\n")

if args[:2] == ["d1", "execute"]:
    if (root / "d1_fails").exists():
        print("no such table: episodes", file=sys.stderr)
        raise SystemExit(1)
    print(json.dumps([{"results": [], "success": True}]))
    raise SystemExit(0)

if args[:3] == ["r2", "object", "get"]:
    source = root / "objects" / args[3].split("/", 1)[1]
    destination = Path(args[args.index("--file") + 1])
    if not source.is_file():
        print(f"The specified key does not exist: {args[3]}", file=sys.stderr)
        raise SystemExit(1)
    destination.parent.mkdir(parents=True, exist_ok=True)
    shutil.copyfile(source, destination)
    raise SystemExit(0)

if args[:3] == ["r2", "object", "put"]:
    destination = root / "objects" / args[3].split("/", 1)[1]
    source = Path(args[args.index("--file") + 1])
    destination.parent.mkdir(parents=True, exist_ok=True)
    shutil.copyfile(source, destination)
    raise SystemExit(0)

print(f"unexpected wrangler invocation: {args}", file=sys.stderr)
raise SystemExit(2)
'''


class _Listing(http.server.BaseHTTPRequestHandler):
    """Serve the R2 object listing straight out of the fake object tree."""

    root: ClassVar[Path] = Path()

    def do_GET(self) -> None:  # noqa: N802 - BaseHTTPRequestHandler's name
        match = re.match(
            rf"^/accounts/{ACCOUNT_ID}/r2/buckets/([^/]+)/objects\?(.*)$", self.path
        )
        if match is None or self.headers.get("Authorization") != f"Bearer {API_TOKEN}":
            self.send_response(403)
            self.end_headers()
            return
        prefix = parse_qs(match.group(2)).get("prefix", [""])[0]
        objects = _describe_objects(type(self).root, prefix)
        body = json.dumps(
            {
                "success": True,
                "errors": [],
                "result": objects,
                "result_info": {"is_truncated": False, "cursor": ""},
            }
        ).encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        _ = self.wfile.write(body)

    @override
    def log_message(self, format: str, *args: Any) -> None:  # noqa: A002
        return


def _describe_objects(root: Path, prefix: str) -> list[dict[str, Any]]:
    objects_root = root / "objects"
    metadata: dict[str, dict[str, str]] = {}
    metadata_file = root / "metadata.json"
    if metadata_file.is_file():
        metadata = json.loads(metadata_file.read_text(encoding="utf-8"))
    described: list[dict[str, Any]] = []
    for path in sorted(objects_root.rglob("*")):
        if not path.is_file():
            continue
        key = path.relative_to(objects_root).as_posix()
        if not key.startswith(prefix):
            continue
        described.append(
            {
                "key": key,
                "size": path.stat().st_size,
                "last_modified": UPLOADED_AT,
                "custom_metadata": metadata.get(key, {}),
            }
        )
    return described


@pytest.fixture
def account(tmp_path: Path) -> Iterator[tuple[Path, dict[str, str]]]:
    """A fake account: an object tree, a listing server, and a fake wrangler."""
    root = tmp_path / "r2"
    (root / "objects").mkdir(parents=True)

    handler = tmp_path / "fake_wrangler.py"
    handler.write_text(FAKE_WRANGLER, encoding="utf-8")
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    npx = bin_dir / "npx"
    npx.write_text(
        f'#!/bin/sh\nexec "{sys.executable}" "{handler}" "{root}" "$@"\n',
        encoding="utf-8",
    )
    npx.chmod(0o755)

    _Listing.root = root
    server = http.server.HTTPServer(("127.0.0.1", 0), _Listing)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()

    environment = dict(os.environ)
    environment["PATH"] = f"{bin_dir}{os.pathsep}{environment['PATH']}"
    environment["CLOUDFLARE_ACCOUNT_ID"] = ACCOUNT_ID
    environment["CLOUDFLARE_API_TOKEN"] = API_TOKEN
    environment["API_BASE"] = f"http://127.0.0.1:{server.server_port}"
    try:
        yield root, environment
    finally:
        server.shutdown()
        server.server_close()


def seed_raw_chunks(
    root: Path,
    envelopes: list[dict[str, Any]],
    *,
    game_type: str | None = None,
) -> None:
    metadata: dict[str, dict[str, str]] = {}
    metadata_file = root / "metadata.json"
    if metadata_file.is_file():
        metadata = json.loads(metadata_file.read_text(encoding="utf-8"))
    for envelope in envelopes:
        key = (
            f"raw/user={envelope['user_id']}/game={envelope['game_id']}"
            f"/chunk={envelope['chunk_id']}.json"
        )
        path = root / "objects" / key
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(envelope), encoding="utf-8")
        if game_type is not None:
            metadata[key] = {"game_type": game_type}
    metadata_file.write_text(json.dumps(metadata), encoding="utf-8")


def run_script(
    environment: dict[str, str],
    *arguments: str,
    state_file: Path | None = None,
) -> subprocess.CompletedProcess[str]:
    command = [
        sys.executable,
        str(SCRIPT),
        "--api-base",
        environment["API_BASE"],
        *arguments,
    ]
    if state_file is not None and "--state-file" not in arguments:
        command += ["--state-file", str(state_file)]
    return subprocess.run(
        command,
        capture_output=True,
        text=True,
        check=False,
        cwd=ROOT,
        env=environment,
    )


def written_keys(root: Path) -> list[str]:
    objects_root = root / "objects"
    return sorted(
        path.relative_to(objects_root).as_posix()
        for path in objects_root.rglob("*")
        if path.is_file() and path.relative_to(objects_root).as_posix().startswith("episodes/")
    )


def wrangler_calls(root: Path) -> list[list[str]]:
    log = root / "calls.log"
    if not log.is_file():
        return []
    return [json.loads(line) for line in log.read_text(encoding="utf-8").splitlines()]


def complete_episode(root: Path, *, game_type: str | None = None) -> list[dict[str, Any]]:
    envelopes = split_into_chunks(build_events(4, ended=True), [7, 7])
    seed_raw_chunks(root, envelopes, game_type=game_type)
    return envelopes


# --------------------------------------------------------------------------- #
# conversion


def test_converts_a_stranded_game_into_parquet_and_a_manifest(
    account: tuple[Path, dict[str, str]],
) -> None:
    root, environment = account
    _ = complete_episode(root)

    result = run_script(environment, "--game-type", "all", state_file=root / "state.json")
    assert result.returncode == 0, result.stdout + result.stderr

    keys = written_keys(root)
    assert len(keys) == 2
    parquet_key = next(key for key in keys if key.endswith(".parquet"))
    manifest_key = next(key for key in keys if key.endswith(".manifest.json"))
    # The partition date comes from the chunks, not from today.
    assert parquet_key.startswith(f"episodes/parquet/v1/{GAME_TYPE}/2026/07/16/")
    assert parquet_key.endswith(f"{GAME_ID}.parquet")

    manifest = json.loads((root / "objects" / manifest_key).read_text(encoding="utf-8"))
    assert manifest["status"] == "complete"
    assert manifest["user_id"] == USER_ID
    assert manifest["chunk_count"] == 2
    assert manifest["row_count"] == 4
    assert manifest["parquet_key"] == parquet_key

    body = (root / "objects" / parquet_key).read_bytes()
    assert body.startswith(b"PAR1") and body.endswith(b"PAR1")
    assert manifest["parquet_bytes"] == len(body)

    d1 = [call for call in wrangler_calls(root) if call[:2] == ["d1", "execute"]]
    assert len(d1) == 1
    statement = d1[0][d1[0].index("--command") + 1]
    assert "INSERT INTO episodes" in statement
    assert "ON CONFLICT(episode_id) DO UPDATE SET" in statement


def test_an_unfinished_game_is_classified_the_way_the_assembler_would(
    account: tuple[Path, dict[str, str]],
) -> None:
    root, environment = account
    seed_raw_chunks(root, split_into_chunks(build_events(3, ended=False), [10]))

    result = run_script(environment, "--game-type", "all", state_file=root / "state.json")
    assert result.returncode == 0, result.stdout + result.stderr

    manifest_key = next(key for key in written_keys(root) if key.endswith(".manifest.json"))
    manifest = json.loads((root / "objects" / manifest_key).read_text(encoding="utf-8"))
    assert manifest["status"] == "abandoned"


def test_a_gap_in_the_chunk_chain_is_incomplete_not_a_failure(
    account: tuple[Path, dict[str, str]],
) -> None:
    root, environment = account
    envelopes = split_into_chunks(build_events(6, ended=True), [7, 6, 7])
    seed_raw_chunks(root, [envelopes[0], envelopes[2]])

    result = run_script(environment, "--game-type", "all", state_file=root / "state.json")
    assert result.returncode == 0, result.stdout + result.stderr

    manifest_key = next(key for key in written_keys(root) if key.endswith(".manifest.json"))
    manifest = json.loads((root / "objects" / manifest_key).read_text(encoding="utf-8"))
    assert manifest["status"] == "incomplete"
    assert manifest["chunk_count"] == 2


# --------------------------------------------------------------------------- #
# idempotency and resumability


def test_a_second_run_writes_nothing_and_reports_the_game_as_converted(
    account: tuple[Path, dict[str, str]],
) -> None:
    root, environment = account
    _ = complete_episode(root)
    state_file = root / "state.json"

    first = run_script(environment, "--game-type", "all", state_file=state_file)
    assert first.returncode == 0, first.stdout + first.stderr
    before = {key: (root / "objects" / key).read_bytes() for key in written_keys(root)}
    (root / "calls.log").unlink()

    second = run_script(environment, "--game-type", "all", state_file=state_file)
    assert second.returncode == 0, second.stdout + second.stderr

    after = {key: (root / "objects" / key).read_bytes() for key in written_keys(root)}
    assert after == before
    assert not any(call[:3] == ["r2", "object", "put"] for call in wrangler_calls(root))
    assert not any(call[:2] == ["d1", "execute"] for call in wrangler_calls(root))
    assert "games already converted: 1" in second.stdout


def test_force_re_derives_but_still_does_not_rewrite_identical_artifacts(
    account: tuple[Path, dict[str, str]],
) -> None:
    root, environment = account
    _ = complete_episode(root)
    state_file = root / "state.json"

    assert run_script(environment, "--game-type", "all", state_file=state_file).returncode == 0
    before = {key: (root / "objects" / key).read_bytes() for key in written_keys(root)}
    (root / "calls.log").unlink()

    forced = run_script(environment, "--game-type", "all", "--force", state_file=state_file)
    assert forced.returncode == 0, forced.stdout + forced.stderr
    assert {key: (root / "objects" / key).read_bytes() for key in written_keys(root)} == before
    assert not any(call[:3] == ["r2", "object", "put"] for call in wrangler_calls(root))
    assert "R2 unchanged" in forced.stdout
    # The index is still refreshed: R2 being correct says nothing about D1.
    assert any(call[:2] == ["d1", "execute"] for call in wrangler_calls(root))


def test_force_skips_entirely_when_the_index_is_not_being_written(
    account: tuple[Path, dict[str, str]],
) -> None:
    root, environment = account
    _ = complete_episode(root)
    state_file = root / "state.json"

    assert run_script(environment, "--game-type", "all", state_file=state_file).returncode == 0
    (root / "calls.log").unlink()

    forced = run_script(
        environment, "--game-type", "all", "--force", "--skip-index", state_file=state_file
    )
    assert forced.returncode == 0, forced.stdout + forced.stderr
    assert "already written with identical artifacts" in forced.stdout
    assert not any(call[:3] == ["r2", "object", "put"] for call in wrangler_calls(root))


def test_force_corrects_a_stale_manifest_without_touching_the_parquet(
    account: tuple[Path, dict[str, str]],
) -> None:
    """The already-written-episode repair path: manifest and D1 only."""
    root, environment = account
    _ = complete_episode(root)
    state_file = root / "state.json"
    assert run_script(environment, "--game-type", "all", state_file=state_file).returncode == 0

    parquet_key = next(key for key in written_keys(root) if key.endswith(".parquet"))
    manifest_key = next(key for key in written_keys(root) if key.endswith(".manifest.json"))
    parquet_before = (root / "objects" / parquet_key).read_bytes()
    manifest = json.loads((root / "objects" / manifest_key).read_text(encoding="utf-8"))
    # Stand in for an episode written by an assembler that classified it wrongly.
    manifest["status"] = "abandoned"
    (root / "objects" / manifest_key).write_text(json.dumps(manifest), encoding="utf-8")
    (root / "calls.log").unlink()

    forced = run_script(environment, "--game-type", "all", "--force", state_file=state_file)
    assert forced.returncode == 0, forced.stdout + forced.stderr

    puts = [call for call in wrangler_calls(root) if call[:3] == ["r2", "object", "put"]]
    assert [call[3] for call in puts] == [f"{BUCKET}/{manifest_key}"]
    assert (root / "objects" / parquet_key).read_bytes() == parquet_before

    repaired = json.loads((root / "objects" / manifest_key).read_text(encoding="utf-8"))
    assert repaired["status"] == "complete"
    # The Parquet object was never rewritten, so its digest must not have moved.
    assert repaired["parquet_sha256"] == manifest["parquet_sha256"]
    assert "manifest only, parquet_sha256 preserved" in forced.stdout


def test_parquet_bytes_are_deterministic_across_runs(
    account: tuple[Path, dict[str, str]],
) -> None:
    root, environment = account
    _ = complete_episode(root)

    assert run_script(
        environment, "--game-type", "all", state_file=root / "state.json"
    ).returncode == 0
    parquet_key = next(key for key in written_keys(root) if key.endswith(".parquet"))
    first = (root / "objects" / parquet_key).read_bytes()

    for key in written_keys(root):
        (root / "objects" / key).unlink()
    assert run_script(
        environment, "--game-type", "all", state_file=root / "state2.json"
    ).returncode == 0
    assert (root / "objects" / parquet_key).read_bytes() == first


def test_a_ledgered_game_is_skipped_even_if_its_parquet_is_missing(
    account: tuple[Path, dict[str, str]],
) -> None:
    root, environment = account
    _ = complete_episode(root)
    state_file = root / "state.json"
    state_file.write_text(
        json.dumps({"version": 1, "completed": {GAME_ID: "episodes/parquet/v1/x.parquet"}}),
        encoding="utf-8",
    )

    result = run_script(environment, "--game-type", "all", state_file=state_file)
    assert result.returncode == 0, result.stdout + result.stderr
    assert written_keys(root) == []
    assert "games already converted: 1" in result.stdout


def test_an_unreadable_ledger_is_ignored_rather_than_fatal(
    account: tuple[Path, dict[str, str]],
) -> None:
    root, environment = account
    _ = complete_episode(root)
    state_file = root / "state.json"
    state_file.write_text("{not json", encoding="utf-8")

    result = run_script(environment, "--game-type", "all", state_file=state_file)
    assert result.returncode == 0, result.stdout + result.stderr
    assert len(written_keys(root)) == 2
    assert json.loads(state_file.read_text(encoding="utf-8"))["completed"][GAME_ID]


# --------------------------------------------------------------------------- #
# scope


def test_dry_run_writes_nothing(account: tuple[Path, dict[str, str]]) -> None:
    root, environment = account
    _ = complete_episode(root)
    state_file = root / "state.json"

    result = run_script(environment, "--game-type", "all", "--dry-run", state_file=state_file)
    assert result.returncode == 0, result.stdout + result.stderr
    assert written_keys(root) == []
    assert not state_file.exists()
    assert "would write" in result.stdout
    assert "episodes to write: 1" in result.stdout
    assert not any(call[:3] == ["r2", "object", "put"] for call in wrangler_calls(root))


def test_game_type_filter_reads_the_body_when_metadata_is_absent(
    account: tuple[Path, dict[str, str]],
) -> None:
    root, environment = account
    _ = complete_episode(root)

    excluded = run_script(environment, "--game-type", "snake", state_file=root / "state.json")
    assert excluded.returncode == 0, excluded.stdout + excluded.stderr
    assert written_keys(root) == []
    assert "games out of scope: 1" in excluded.stdout

    included = run_script(environment, "--game-type", GAME_TYPE, state_file=root / "state.json")
    assert included.returncode == 0, included.stdout + included.stderr
    assert len(written_keys(root)) == 2


def test_dry_run_uses_custom_metadata_to_filter_without_reading_objects(
    account: tuple[Path, dict[str, str]],
) -> None:
    root, environment = account
    _ = complete_episode(root, game_type=GAME_TYPE)

    result = run_script(
        environment, "--game-type", "snake", "--dry-run", state_file=root / "state.json"
    )
    assert result.returncode == 0, result.stdout + result.stderr
    assert "games out of scope: 1" in result.stdout
    assert not any(call[:3] == ["r2", "object", "get"] for call in wrangler_calls(root))


def test_explicit_game_ids_narrow_the_run(account: tuple[Path, dict[str, str]]) -> None:
    root, environment = account
    _ = complete_episode(root)

    result = run_script(
        environment,
        "--game-type",
        "all",
        "--game-id",
        "game_018f0000-0000-7000-8000-00000000ffff",
        state_file=root / "state.json",
    )
    assert result.returncode == 0, result.stdout + result.stderr
    assert written_keys(root) == []
    assert "games out of scope: 1" in result.stdout


@pytest.mark.parametrize(
    ("arguments", "converted"),
    [
        (["--since", "2026-07-27"], False),
        (["--until", "2026-07-25"], False),
        (["--since", "2026-07-26", "--until", "2026-07-26"], True),
    ],
)
def test_upload_date_window(
    account: tuple[Path, dict[str, str]],
    arguments: list[str],
    converted: bool,
) -> None:
    root, environment = account
    _ = complete_episode(root)

    result = run_script(
        environment, "--game-type", "all", *arguments, state_file=root / "state.json"
    )
    assert result.returncode == 0, result.stdout + result.stderr
    assert bool(written_keys(root)) is converted


def test_limit_bounds_the_run(account: tuple[Path, dict[str, str]]) -> None:
    root, environment = account
    _ = complete_episode(root)

    result = run_script(
        environment, "--game-type", "all", "--limit", "1", state_file=root / "state.json"
    )
    assert result.returncode == 0, result.stdout + result.stderr
    assert len(written_keys(root)) == 2


# --------------------------------------------------------------------------- #
# failure handling


def test_a_missing_episodes_table_fails_the_game_with_the_migration_hint(
    account: tuple[Path, dict[str, str]],
) -> None:
    root, environment = account
    _ = complete_episode(root)
    (root / "d1_fails").touch()

    result = run_script(environment, "--game-type", "all", state_file=root / "state.json")
    assert result.returncode == 1
    assert "0003_episodes_index.sql" in result.stdout
    # R2 was still written first, so the artifacts survive the failed index.
    assert len(written_keys(root)) == 2
    assert not (root / "state.json").exists()


def test_skip_index_writes_r2_only(account: tuple[Path, dict[str, str]]) -> None:
    root, environment = account
    _ = complete_episode(root)
    (root / "d1_fails").touch()

    result = run_script(
        environment, "--game-type", "all", "--skip-index", state_file=root / "state.json"
    )
    assert result.returncode == 0, result.stdout + result.stderr
    assert len(written_keys(root)) == 2
    assert not any(call[:2] == ["d1", "execute"] for call in wrangler_calls(root))


def test_a_corrupt_chunk_fails_only_its_own_game(
    account: tuple[Path, dict[str, str]],
) -> None:
    root, environment = account
    envelopes = complete_episode(root)
    key = (
        f"raw/user={USER_ID}/game={GAME_ID}/chunk={envelopes[0]['chunk_id']}.json"
    )
    (root / "objects" / key).write_text("{}", encoding="utf-8")

    result = run_script(environment, "--game-type", "all", state_file=root / "state.json")
    assert result.returncode == 1
    assert "is not a valid v2 chunk" in result.stdout
    assert written_keys(root) == []


def test_raw_objects_shared_between_users_are_refused(
    account: tuple[Path, dict[str, str]],
) -> None:
    root, environment = account
    envelopes = complete_episode(root)
    other = f"raw/user=usr_someone-else/game={GAME_ID}/chunk={envelopes[0]['chunk_id']}.json"
    path = root / "objects" / other
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(envelopes[0]), encoding="utf-8")

    result = run_script(environment, "--game-type", "all", state_file=root / "state.json")
    assert result.returncode == 1
    assert "more than one user" in result.stdout
    assert written_keys(root) == []


# --------------------------------------------------------------------------- #
# contract drift


def test_wrangler_pin_matches_the_makefile() -> None:
    import importlib.util

    spec = importlib.util.spec_from_file_location("backfill_episodes", SCRIPT)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)

    match = re.search(r"^WRANGLER_VERSION \?= (.+)$", MAKEFILE.read_text(), re.MULTILINE)
    assert match is not None
    assert match.group(1).strip() == module.WRANGLER_VERSION


def test_the_upsert_names_the_same_columns_as_the_worker() -> None:
    import importlib.util

    from retro_backend.storage import d1

    spec = importlib.util.spec_from_file_location("backfill_episodes", SCRIPT)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)

    worker_sql = d1._UPSERT_EPISODE_SQL  # pyright: ignore[reportPrivateUsage]
    header = worker_sql.split("VALUES", 1)[0]
    columns = tuple(
        name.strip()
        for name in header.split("(", 1)[1].rsplit(")", 1)[0].replace("\n", " ").split(",")
    )
    assert columns == module.UPSERT_EPISODE_COLUMNS


@pytest.mark.parametrize(
    ("arguments", "message"),
    [
        (["--game-type", "all", "--game-type", "2048"], "cannot be combined"),
        (["--game-type", ""], "empty value"),
        (["--game-type", "all", "--game-id", "not-a-game"], "not a valid game id"),
        (["--game-type", "all", "--since", "2026-08-02", "--until", "2026-08-01"], "is after"),
        (["--game-type", "all", "--limit", "0"], "positive integer"),
    ],
)
def test_argument_validation(
    account: tuple[Path, dict[str, str]],
    arguments: list[str],
    message: str,
) -> None:
    root, environment = account
    result = run_script(environment, *arguments, state_file=root / "state.json")
    assert result.returncode == 2
    assert message in result.stderr


def test_a_missing_api_token_is_reported_before_anything_is_read(
    account: tuple[Path, dict[str, str]],
) -> None:
    root, environment = account
    environment = dict(environment)
    del environment["CLOUDFLARE_API_TOKEN"]

    result = run_script(environment, "--game-type", "all", state_file=root / "state.json")
    assert result.returncode == 2
    assert "CLOUDFLARE_API_TOKEN" in result.stderr
