"""Pull episode Parquet files out of R2 into a local tree PyTorch can iterate.

The writer side of the pipeline lands one Parquet object plus a JSON manifest
sidecar per episode:

    episodes/parquet/v1/{game_type}/{YYYY}/{MM}/{DD}/{game_id}.parquet
    episodes/parquet/v1/{game_type}/{YYYY}/{MM}/{DD}/{game_id}.manifest.json

This script mirrors that partitioning locally, flattening the date into one
directory level so a torch ``Dataset`` can glob it:

    {output_dir}/{game_type}/{YYYY-MM-DD}/{game_id}.parquet
    {output_dir}/{game_type}/{YYYY-MM-DD}/{game_id}.manifest.json

``wrangler`` has no R2 object-listing command (``wrangler r2 object`` exposes
only get/put/delete), so the set of episodes to fetch comes from the D1
``episodes`` index instead, which carries the ``parquet_key`` for every object
that was successfully written. Date filtering is applied to the ``{YYYY}/{MM}/{DD}``
segments of that key, not to the D1 timestamp, so the filter always describes the
tree you actually get.

Every Parquet file is verified against ``parquet_sha256`` from its manifest
before it is moved into place; a mismatch is reported and the object is left out
of the tree rather than silently accepted. A file already present locally with a
matching digest is skipped, so re-running the same command resumes.

Examples::

    python scripts/download-r2-dataset.py --game-type all --output-dir data/episodes
    python scripts/download-r2-dataset.py --game-type 2048 --game-type snake \\
        --output-dir data/episodes --since 2026-07-01 --until 2026-07-31 --remote
"""

import argparse
import hashlib
import json
import os
import re
import subprocess
import sys
import tempfile
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from datetime import date, datetime
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]

DEFAULT_BUCKET = "retro-gameplay-raw"
DEFAULT_DATABASE = "retro-backend"
DEFAULT_PREFIX = "episodes/parquet/v1"
DEFAULT_REMOTE_CONFIG = "wrangler.production.toml"
# Keep in sync with WRANGLER_VERSION in the Makefile (tests assert they match).
WRANGLER_VERSION = "4.112.0"

ALL_GAME_TYPES = "all"
SHA256_PATTERN = re.compile(r"^[0-9a-f]{64}$")
READ_BLOCK_BYTES = 1024 * 1024

CATALOG_COLUMNS = (
    "game_type",
    "game_id",
    "completed_at_ms",
    "parquet_key",
    "manifest_key",
    "parquet_bytes",
    "parquet_sha256",
)


class DownloadFailure(RuntimeError):
    """A failure that aborts the whole run rather than one episode."""


@dataclass(frozen=True)
class Scope:
    """Where ``wrangler`` should look for D1 and R2: the dev sim, or the account."""

    remote: bool
    persist_to: str | None
    config: str

    def d1_flags(self) -> list[str]:
        if not self.remote:
            return ["--local", *(["--persist-to", self.persist_to] if self.persist_to else [])]
        return ["--remote", "--config", self.config]

    def r2_flags(self) -> list[str]:
        if not self.remote:
            return ["--local", *(["--persist-to", self.persist_to] if self.persist_to else [])]
        return ["--remote"]


@dataclass(frozen=True)
class Episode:
    """One episode to fetch, as resolved from the D1 catalog and its R2 key."""

    game_type: str
    game_id: str
    day: date
    parquet_key: str
    manifest_key: str
    parquet_bytes: int | None
    catalog_sha256: str | None

    def local_dir(self, output_dir: Path) -> Path:
        return output_dir / self.game_type / self.day.isoformat()

    def parquet_path(self, output_dir: Path) -> Path:
        return self.local_dir(output_dir) / f"{self.game_id}.parquet"

    def manifest_path(self, output_dir: Path) -> Path:
        return self.local_dir(output_dir) / f"{self.game_id}.manifest.json"


@dataclass
class Summary:
    downloaded: int = 0
    skipped: int = 0
    failed: int = 0
    total_bytes: int = 0


def ok(message: str) -> None:
    print(f"  \033[32m✓\033[0m {message}", flush=True)


def info(message: str) -> None:
    print(f"  \033[34m·\033[0m {message}", flush=True)


def warn(message: str) -> None:
    print(f"  \033[33m!\033[0m {message}", flush=True)


def fail(message: str) -> None:
    print(f"  \033[31m✗\033[0m {message}", flush=True)


def wrangler(*args: str) -> subprocess.CompletedProcess[str]:
    command = ["npx", "--yes", f"wrangler@{WRANGLER_VERSION}", *args]
    return subprocess.run(
        command,
        cwd=ROOT,
        capture_output=True,
        text=True,
        check=False,
    )


def sql_quote(value: str) -> str:
    return "'" + value.replace("'", "''") + "'"


def d1_execute(database: str, sql: str, scope: Scope) -> list[dict[str, Any]]:
    """Run one read-only statement and return its rows."""
    args = ["d1", "execute", database, "--json", "--command", sql, *scope.d1_flags()]
    completed = wrangler(*args)
    if completed.returncode != 0:
        detail = completed.stderr.strip() or completed.stdout.strip()
        raise DownloadFailure(f"wrangler d1 execute failed ({completed.returncode}):\n{detail}")
    # Wrangler prints human-readable preamble lines before the JSON payload.
    text = completed.stdout
    start = text.find("[")
    if start == -1:
        return []
    try:
        payload: object = json.loads(text[start:])
    except json.JSONDecodeError as error:
        raise DownloadFailure(f"could not parse d1 --json output:\n{text}") from error
    rows: list[dict[str, Any]] = []
    for block in payload if isinstance(payload, list) else []:
        results = block.get("results") if isinstance(block, dict) else None
        if isinstance(results, list):
            rows.extend(row for row in results if isinstance(row, dict))
    return rows


def catalog_sql(game_types: frozenset[str] | None) -> str:
    """Build the episodes query, pushing the game-type filter into D1."""
    where = ""
    if game_types is not None:
        quoted = ", ".join(sql_quote(value) for value in sorted(game_types))
        where = f" WHERE game_type IN ({quoted})"
    columns = ", ".join(CATALOG_COLUMNS)
    return f"SELECT {columns} FROM episodes{where} ORDER BY completed_at_ms ASC, game_id ASC;"


def key_pattern(prefix: str) -> re.Pattern[str]:
    return re.compile(
        re.escape(prefix.strip("/"))
        + r"/(?P<game_type>[^/]+)"
        + r"/(?P<year>\d{4})/(?P<month>\d{2})/(?P<day>\d{2})"
        + r"/(?P<game_id>[^/]+)\.parquet$"
    )


def safe_component(value: str) -> bool:
    """Reject anything that would escape or hide inside the output directory."""
    return value not in {"", ".", ".."} and "/" not in value and not value.startswith(".")


def optional_int(value: object) -> int | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value
    if isinstance(value, str) and value.isdigit():
        return int(value)
    return None


def normalized_sha256(value: object) -> str | None:
    if not isinstance(value, str):
        return None
    candidate = value.strip().lower()
    return candidate if SHA256_PATTERN.fullmatch(candidate) else None


def parse_catalog_row(row: dict[str, Any], pattern: re.Pattern[str]) -> Episode:
    """Turn one D1 row into an Episode, trusting the key over the other columns."""
    parquet_key = row.get("parquet_key")
    if not isinstance(parquet_key, str) or not parquet_key:
        raise ValueError("row has no parquet_key")
    match = pattern.search(parquet_key)
    if match is None:
        raise ValueError(f"parquet_key does not match the expected layout: {parquet_key}")

    game_type = match.group("game_type")
    game_id = match.group("game_id")
    if not safe_component(game_type) or not safe_component(game_id):
        raise ValueError(f"parquet_key holds an unusable path component: {parquet_key}")
    try:
        day = date(int(match.group("year")), int(match.group("month")), int(match.group("day")))
    except ValueError as error:
        raise ValueError(f"parquet_key holds an impossible date: {parquet_key}") from error

    manifest_key = row.get("manifest_key")
    if not isinstance(manifest_key, str) or not manifest_key:
        manifest_key = parquet_key.removesuffix(".parquet") + ".manifest.json"

    return Episode(
        game_type=game_type,
        game_id=game_id,
        day=day,
        parquet_key=parquet_key,
        manifest_key=manifest_key,
        parquet_bytes=optional_int(row.get("parquet_bytes")),
        catalog_sha256=normalized_sha256(row.get("parquet_sha256")),
    )


def select_episodes(
    rows: Sequence[dict[str, Any]],
    *,
    prefix: str,
    game_types: frozenset[str] | None,
    since: date | None,
    until: date | None,
    limit: int | None,
) -> tuple[list[Episode], list[str]]:
    """Apply the path, game-type and date filters; report unusable rows."""
    pattern = key_pattern(prefix)
    episodes: list[Episode] = []
    rejected: list[str] = []
    for row in rows:
        try:
            episode = parse_catalog_row(row, pattern)
        except ValueError as error:
            rejected.append(str(error))
            continue
        # D1 already filtered on game_type; re-check so a stale or hand-edited
        # catalog cannot widen the download beyond what was asked for.
        if game_types is not None and episode.game_type not in game_types:
            continue
        if since is not None and episode.day < since:
            continue
        if until is not None and episode.day > until:
            continue
        episodes.append(episode)
        if limit is not None and len(episodes) >= limit:
            break
    return episodes, rejected


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        while True:
            block = handle.read(READ_BLOCK_BYTES)
            if not block:
                break
            digest.update(block)
    return digest.hexdigest()


def get_object(bucket: str, key: str, destination: Path, scope: Scope) -> str | None:
    """Fetch one R2 object to ``destination``; return an error string on failure."""
    args = ["r2", "object", "get", f"{bucket}/{key}", "--file", str(destination), *scope.r2_flags()]
    completed = wrangler(*args)
    if completed.returncode != 0:
        detail = completed.stderr.strip() or completed.stdout.strip()
        return detail or f"wrangler exited {completed.returncode}"
    if not destination.exists():
        return "wrangler reported success but wrote no file"
    return None


def manifest_sha256(payload: object) -> str | None:
    if not isinstance(payload, dict):
        return None
    return normalized_sha256(payload.get("parquet_sha256"))


def load_local_manifest(path: Path) -> object | None:
    if not path.is_file():
        return None
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None


def fetch_manifest(
    episode: Episode,
    bucket: str,
    scope: Scope,
    staging: Path,
) -> tuple[Path | None, str | None]:
    """Fetch the manifest sidecar into ``staging`` and check that it parses."""
    destination = staging / f"{episode.game_id}.manifest.json"
    error = get_object(bucket, episode.manifest_key, destination, scope)
    if error is not None:
        return None, error
    try:
        payload: object = json.loads(destination.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as parse_error:
        return None, f"manifest is not readable JSON: {parse_error}"
    if manifest_sha256(payload) is None:
        return None, "manifest has no usable parquet_sha256"
    return destination, None


def download_episode(
    episode: Episode,
    *,
    bucket: str,
    scope: Scope,
    output_dir: Path,
    keep_manifests: bool,
    summary: Summary,
) -> None:
    label = f"{episode.game_type}/{episode.day.isoformat()}/{episode.game_id}"
    parquet_path = episode.parquet_path(output_dir)
    manifest_path = episode.manifest_path(output_dir)
    parquet_path.parent.mkdir(parents=True, exist_ok=True)

    with tempfile.TemporaryDirectory(dir=parquet_path.parent, prefix=".staging-") as directory:
        staging = Path(directory)
        # A sidecar already on disk lets a resume run skip the network entirely.
        expected = manifest_sha256(load_local_manifest(manifest_path))
        staged_manifest: Path | None = None
        if expected is None:
            staged_manifest, error = fetch_manifest(episode, bucket, scope, staging)
            if staged_manifest is None:
                expected = episode.catalog_sha256
                if expected is None:
                    summary.failed += 1
                    fail(f"{label}: no usable manifest and no catalog checksum ({error})")
                    return
                warn(f"{label}: manifest unusable ({error}); falling back to the catalog checksum")
            else:
                expected = manifest_sha256(load_local_manifest(staged_manifest))
                assert expected is not None  # fetch_manifest already validated it

        if parquet_path.is_file():
            if sha256_file(parquet_path) == expected:
                summary.skipped += 1
                ok(f"{label}: already present with a matching checksum")
                if keep_manifests and staged_manifest is not None:
                    os.replace(staged_manifest, manifest_path)
                return
            warn(f"{label}: local copy does not match {expected[:12]}…; downloading again")

        staged_parquet = staging / f"{episode.game_id}.parquet"
        error = get_object(bucket, episode.parquet_key, staged_parquet, scope)
        if error is not None:
            summary.failed += 1
            fail(f"{label}: download failed ({error})")
            return
        actual = sha256_file(staged_parquet)
        if actual != expected:
            summary.failed += 1
            fail(f"{label}: checksum mismatch, discarded (expected {expected}, got {actual})")
            return
        size = staged_parquet.stat().st_size

        if keep_manifests and staged_manifest is None and not manifest_path.is_file():
            staged_manifest, error = fetch_manifest(episode, bucket, scope, staging)
            if staged_manifest is None:
                warn(f"{label}: parquet is verified, but its manifest was not stored ({error})")
        os.replace(staged_parquet, parquet_path)
        if keep_manifests and staged_manifest is not None:
            os.replace(staged_manifest, manifest_path)

    summary.downloaded += 1
    summary.total_bytes += size
    ok(f"{label}: downloaded {size} bytes")


def plan_episode(episode: Episode, output_dir: Path, summary: Summary) -> None:
    """Report what a real run would do, without touching R2 or the output tree."""
    label = f"{episode.game_type}/{episode.day.isoformat()}/{episode.game_id}"
    parquet_path = episode.parquet_path(output_dir)
    expected = manifest_sha256(load_local_manifest(episode.manifest_path(output_dir)))
    if expected is None:
        expected = episode.catalog_sha256
    if parquet_path.is_file() and expected is not None and sha256_file(parquet_path) == expected:
        summary.skipped += 1
        ok(f"{label}: would skip, already present with a matching checksum")
        return
    summary.downloaded += 1
    summary.total_bytes += episode.parquet_bytes or 0
    info(f"{label}: would download {episode.parquet_key} -> {parquet_path}")


def prune_empty_dirs(output_dir: Path) -> None:
    """Drop partition directories that a failed episode left behind empty."""
    for directory in sorted(output_dir.rglob("*"), key=lambda path: len(path.parts), reverse=True):
        if directory.is_dir() and not any(directory.iterdir()):
            directory.rmdir()


def print_summary(summary: Summary, *, dry_run: bool) -> None:
    downloaded = "episodes to download" if dry_run else "episodes downloaded"
    skipped = "episodes already present" if dry_run else "episodes skipped"
    size = "total bytes to download" if dry_run else "total bytes downloaded"
    print("\nSummary", flush=True)
    print(f"  {downloaded}: {summary.downloaded}", flush=True)
    print(f"  {skipped}: {summary.skipped}", flush=True)
    print(f"  episodes failed: {summary.failed}", flush=True)
    print(f"  {size}: {summary.total_bytes}", flush=True)


def parse_date(value: str) -> date:
    try:
        return datetime.strptime(value, "%Y-%m-%d").date()
    except ValueError:
        raise argparse.ArgumentTypeError(
            f"expected a UTC date as YYYY-MM-DD, got {value!r}"
        ) from None


def positive_int(value: str) -> int:
    try:
        parsed = int(value)
    except ValueError:
        raise argparse.ArgumentTypeError(f"expected an integer, got {value!r}") from None
    if parsed < 1:
        raise argparse.ArgumentTypeError(f"expected a positive integer, got {value!r}")
    return parsed


def resolve_game_types(values: Iterable[str]) -> frozenset[str] | None:
    """``None`` means every game type; otherwise the explicit set."""
    selected = {value.strip() for value in values}
    if ALL_GAME_TYPES in selected:
        if len(selected) > 1:
            raise argparse.ArgumentTypeError(
                "--game-type all cannot be combined with specific game types"
            )
        return None
    if "" in selected:
        raise argparse.ArgumentTypeError("--game-type does not accept an empty value")
    return frozenset(selected)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Download episode Parquet files from R2 into a local training tree.",
    )
    parser.add_argument(
        "--game-type",
        action="append",
        required=True,
        metavar="TYPE",
        help="Game type to download; repeat for several, or pass `all`.",
    )
    parser.add_argument(
        "--output-dir",
        required=True,
        type=Path,
        help="Local root; the R2 partitioning is mirrored underneath it.",
    )
    parser.add_argument("--since", type=parse_date, default=None, metavar="YYYY-MM-DD")
    parser.add_argument("--until", type=parse_date, default=None, metavar="YYYY-MM-DD")
    parser.add_argument("--bucket", default=DEFAULT_BUCKET)
    parser.add_argument("--database", default=DEFAULT_DATABASE)
    parser.add_argument(
        "--prefix",
        default=DEFAULT_PREFIX,
        help="R2 key prefix the episode writer uses.",
    )
    parser.add_argument("--limit", type=positive_int, default=None)
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Report what would be downloaded without fetching anything.",
    )
    manifests = parser.add_mutually_exclusive_group()
    manifests.add_argument(
        "--manifests",
        dest="manifests",
        action="store_true",
        default=True,
        help="Store the .manifest.json sidecar next to each Parquet file (default).",
    )
    manifests.add_argument(
        "--no-manifests",
        dest="manifests",
        action="store_false",
        help="Fetch manifests for verification only; do not keep them locally.",
    )
    parser.add_argument(
        "--remote",
        action="store_true",
        help="Read the account's real D1 and R2 instead of the local dev sim.",
    )
    parser.add_argument(
        "--persist-to",
        default=None,
        help="Local persistence directory, if `wrangler dev` was given one.",
    )
    parser.add_argument(
        "--config",
        default=DEFAULT_REMOTE_CONFIG,
        help="Wrangler config used to resolve remote D1 (--remote only).",
    )
    return parser


def run(arguments: argparse.Namespace, parser: argparse.ArgumentParser) -> int:
    try:
        game_types = resolve_game_types(arguments.game_type)
    except argparse.ArgumentTypeError as error:
        parser.error(str(error))
    since: date | None = arguments.since
    until: date | None = arguments.until
    if since is not None and until is not None and since > until:
        parser.error(f"--since {since.isoformat()} is after --until {until.isoformat()}")

    output_dir: Path = arguments.output_dir
    scope = Scope(
        remote=arguments.remote,
        persist_to=arguments.persist_to,
        config=arguments.config,
    )

    selection = "all game types" if game_types is None else ", ".join(sorted(game_types))
    window = f"{since or 'beginning'} .. {until or 'now'}"
    where = "remote" if scope.remote else "local"
    print(f"Episodes: {selection}   dates: {window}   source: {where} {arguments.bucket}")

    rows = d1_execute(arguments.database, catalog_sql(game_types), scope)
    episodes, rejected = select_episodes(
        rows,
        prefix=arguments.prefix,
        game_types=game_types,
        since=since,
        until=until,
        limit=arguments.limit,
    )
    summary = Summary()
    for reason in rejected:
        summary.failed += 1
        fail(f"unusable catalog row: {reason}")

    print(f"{len(episodes)} episode(s) match after filtering {len(rows)} catalog row(s).")
    if not arguments.dry_run:
        output_dir.mkdir(parents=True, exist_ok=True)

    for episode in episodes:
        if arguments.dry_run:
            plan_episode(episode, output_dir, summary)
        else:
            download_episode(
                episode,
                bucket=arguments.bucket,
                scope=scope,
                output_dir=output_dir,
                keep_manifests=arguments.manifests,
                summary=summary,
            )

    if not arguments.dry_run:
        prune_empty_dirs(output_dir)
    print_summary(summary, dry_run=arguments.dry_run)
    return 1 if summary.failed else 0


def main(argv: Sequence[str] | None = None) -> int:
    parser = build_parser()
    arguments = parser.parse_args(argv)
    try:
        return run(arguments, parser)
    except DownloadFailure as error:
        print(f"\n\033[31mFAIL\033[0m {error}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
