"""Convert gameplay chunks stranded in ``raw/`` into episode Parquet objects.

The episode assembler and the Parquet writer only ever see chunks that arrive
live through ``retro-gameplay-events`` -> ``retro-episode-events``. Everything
uploaded before those were deployed still sits in ``raw/`` with no episode. This
script walks ``raw/``, replays assembly for each stranded game, and writes the
same artifacts the live consumer writes:

    episodes/parquet/v1/{game_type}/{YYYY}/{MM}/{DD}/{game_id}.parquet
    episodes/parquet/v1/{game_type}/{YYYY}/{MM}/{DD}/{game_id}.manifest.json
    one `episodes` row in D1

Assembly is driven **directly**, in this process, rather than by republishing
``episode.ready`` onto ``retro-episode-events``. The deciding reason is the
input the two paths read. ``EpisodeConsumer`` loads its rows from ``validated/``
(:func:`retro_backend.episode_rows.load_episode_rows`), which
``cloudflare/r2-lifecycle.json`` expires after seven days and which treats an
absent object as a silent gap. ``raw/`` is the ninety-day copy of record. A
queue replay of history older than a week would therefore write short or empty
Parquet and index it in D1 as if it were whole. This script reads ``raw/``
instead and reuses the real code — :class:`EpisodeState` for the terminal
classification, :func:`build_rows` for the behaviour-cloning join,
:func:`write_parquet` and :func:`build_manifest` for the artifacts — so only the
transport differs from production, never the semantics.

Two smaller consequences of driving assembly here fall out of the same choice:
``completed_at_ms`` comes from the last chunk's ``created_at_ms`` rather than
from a wall clock, so a July episode lands in a July partition instead of
today's; and nothing has to be added as a second producer on
``retro-episode-events``, which today has exactly one producer and one consumer.

``wrangler`` has no R2 object-listing command, so the ``raw/`` and
``episodes/parquet/v1/`` inventories come from the Cloudflare REST API, which
needs ``CLOUDFLARE_API_TOKEN`` and ``CLOUDFLARE_ACCOUNT_ID`` in the environment.
Individual objects are read and written through ``wrangler`` exactly as
``scripts/download-r2-dataset.py`` does.

Re-running is safe. A game whose Parquet object already exists is skipped, so
the run converges, and ``--state-file`` additionally remembers finished games so
an interrupted run resumes without re-reading them.

``--force`` opts into the second mode: re-deriving games that already have
Parquet, in order to correct what was recorded about them. It compares the two
artifacts independently. An episode whose only defect is its recorded ``status``
has perfectly good Parquet bytes — a zero-row file is an accurate rendering of a
session with no moves — so the Parquet object and its ``parquet_sha256`` are
left alone and only the manifest and the D1 row are rewritten. The corrected
status is re-derived from the chunks by the same :class:`EpisodeState` the
Worker runs; nothing here pattern-matches on a symptom such as ``row_count = 0``.

**Prerequisites.**

1. ``migrations/0003_episodes_index.sql`` must have been applied to the
   production D1 database. It has not been, so every episode write currently
   fails its D1 upsert. Run ``make migrate-remote`` first, or pass
   ``--skip-index`` to write only the R2 artifacts and index them later.
2. For the ``--force`` mode specifically, whatever fixes the classification
   being corrected must already be merged. Status is re-derived from
   :class:`EpisodeState`, so running ``--force`` against an unfixed assembler
   simply reproduces the status that is already stored.

Examples::

    python scripts/backfill-episodes.py --game-type all --dry-run
    python scripts/backfill-episodes.py --game-type 2048 --until 2026-08-03
    python scripts/backfill-episodes.py --game-type all \\
        --game-id game_01482d1e-eaf2-4acb-82b8-fa37544c31a2 --skip-index
"""

import argparse
import hashlib
import json
import os
import re
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.parse
import urllib.request
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import date, datetime
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from retro_backend.consumers.episodes import (  # noqa: E402
    EPISODE_PARQUET_PREFIX,
    build_manifest,
    episode_manifest_key,
    episode_parquet_key,
)
from retro_backend.contracts import (  # noqa: E402
    CHUNK_ID_PATTERN,
    GAME_ID_PATTERN,
    USER_ID_PATTERN,
    EpisodeReady,
    PermanentGameplayError,
    parse_gameplay_chunk,
)
from retro_backend.episode_assembler import (  # noqa: E402
    DEFAULT_EPISODE_MAX_CHUNKS,
    EpisodeChunkRecord,
    EpisodeState,
)
from retro_backend.episode_rows import (  # noqa: E402
    EPISODE_COLUMNS,
    EpisodeConversionError,
    EpisodeRowSet,
    build_rows,
    canonical_json,
    episode_id_for,
)
from retro_backend.parquet import write_parquet  # noqa: E402

DEFAULT_BUCKET = "retro-gameplay-raw"
DEFAULT_DATABASE = "retro-backend"
DEFAULT_RAW_PREFIX = "raw/"
DEFAULT_REMOTE_CONFIG = "wrangler.production.toml"
DEFAULT_API_BASE = "https://api.cloudflare.com/client/v4"
DEFAULT_STATE_FILE = ".backfill-episodes.state.json"
# Keep in sync with WRANGLER_VERSION in the Makefile (tests assert they match).
WRANGLER_VERSION = "4.112.0"

ALL_GAME_TYPES = "all"
STATE_VERSION = 1
LIST_PAGE_SIZE = 1000
LIST_ATTEMPTS = 3
LIST_BACKOFF_SECONDS = 2.0

# A replayed episode is already final: there is no live chunk that could still
# arrive, so the idle window that the Durable Object uses to decide "nothing
# more is coming" collapses to the smallest legal value. This also keeps
# `completed_at_ms` pinned to the last chunk rather than six hours past it.
REPLAY_IDLE_TTL_MS = 1

RAW_KEY_PATTERN = re.compile(
    r"^raw/user=(?P<user_id>[^/]+)/game=(?P<game_id>[^/]+)/chunk=(?P<chunk_id>[^/]+)\.json$"
)
PARQUET_KEY_PATTERN = re.compile(
    re.escape(EPISODE_PARQUET_PREFIX)
    + r"/(?P<game_type>[^/]+)/\d{4}/\d{2}/\d{2}/(?P<game_id>[^/]+)\.parquet$"
)
SHA256_PATTERN = re.compile(r"^[0-9a-f]{64}$")

# Mirrors _UPSERT_EPISODE_SQL in retro_backend.storage.d1. The Worker binds
# parameters; `wrangler d1 execute` only takes a literal statement, so the
# values are quoted below instead. tests/test_backfill_episodes.py asserts the
# two statements name the same columns.
UPSERT_EPISODE_COLUMNS = (
    "episode_id",
    "user_id",
    "game_id",
    "game_type",
    "game_version",
    "status",
    "chunk_count",
    "row_count",
    "first_event_sequence",
    "last_event_sequence",
    "completed_at_ms",
    "parquet_key",
    "parquet_bytes",
    "parquet_sha256",
    "manifest_key",
    "written_at_ms",
)


class BackfillFailure(RuntimeError):
    """A failure that aborts the whole run rather than one game."""


class GameFailure(RuntimeError):
    """A failure confined to one game; the run continues with the next."""


@dataclass(frozen=True)
class Scope:
    """Where the account lives, for both the REST listing and ``wrangler``."""

    account_id: str
    api_token: str
    api_base: str
    config: str

    def d1_flags(self) -> list[str]:
        return ["--remote", "--config", self.config]

    def r2_flags(self) -> list[str]:
        return ["--remote"]


@dataclass(frozen=True)
class RawChunk:
    """One ``raw/`` object as the listing describes it, before it is read."""

    key: str
    user_id: str
    game_id: str
    chunk_id: str
    size: int
    uploaded_on: date | None
    game_type: str | None


@dataclass(frozen=True)
class Game:
    """Every ``raw/`` chunk belonging to one logical ``(user_id, game_id)``."""

    user_id: str
    game_id: str
    chunks: tuple[RawChunk, ...]

    @property
    def label(self) -> str:
        return self.game_id

    @property
    def total_bytes(self) -> int:
        return sum(chunk.size for chunk in self.chunks)

    @property
    def upload_days(self) -> tuple[date, ...]:
        return tuple(chunk.uploaded_on for chunk in self.chunks if chunk.uploaded_on)

    @property
    def declared_game_type(self) -> str | None:
        """``game_type`` from R2 custom metadata, absent on older objects."""
        for chunk in self.chunks:
            if chunk.game_type:
                return chunk.game_type
        return None


@dataclass(frozen=True)
class Conversion:
    """Everything one replayed game will write."""

    episode: EpisodeReady
    episode_id: str
    parquet_key: str
    manifest_key: str
    parquet_body: bytes
    parquet_sha256: str
    rows: EpisodeRowSet

    def manifest(self, *, written_at_ms: int) -> dict[str, object]:
        return build_manifest(
            self.episode,
            self.rows,
            episode_id=self.episode_id,
            parquet_key=self.parquet_key,
            parquet_body=self.parquet_body,
            written_at_ms=written_at_ms,
        )


@dataclass
class Summary:
    converted: int = 0
    skipped: int = 0
    filtered: int = 0
    failed: int = 0
    total_rows: int = 0
    total_bytes: int = 0


@dataclass
class State:
    """The resume ledger: which games this script has already finished."""

    path: Path
    completed: dict[str, str] = field(default_factory=dict)

    @classmethod
    def load(cls, path: Path) -> "State":
        try:
            payload: object = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return cls(path=path)
        if not isinstance(payload, Mapping) or payload.get("version") != STATE_VERSION:
            # An unreadable or foreign ledger is not fatal: the R2 inventory
            # already makes the run converge, the ledger only saves work.
            return cls(path=path)
        completed = payload.get("completed")
        if not isinstance(completed, Mapping):
            return cls(path=path)
        return cls(
            path=path,
            completed={
                key: value
                for key, value in completed.items()
                if isinstance(key, str) and isinstance(value, str)
            },
        )

    def record(self, game_id: str, parquet_key: str) -> None:
        self.completed[game_id] = parquet_key
        self.save()

    def save(self) -> None:
        """Replace the ledger atomically so an interrupt cannot truncate it."""
        payload = {"version": STATE_VERSION, "completed": self.completed}
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with tempfile.NamedTemporaryFile(
            "w",
            encoding="utf-8",
            dir=self.path.parent,
            prefix=".backfill-state-",
            delete=False,
        ) as handle:
            json.dump(payload, handle, sort_keys=True)
            staged = Path(handle.name)
        os.replace(staged, self.path)


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


def _http_json(url: str, token: str) -> Mapping[str, Any]:
    request = urllib.request.Request(url, headers={"Authorization": f"Bearer {token}"})
    last_error: Exception | None = None
    for attempt in range(LIST_ATTEMPTS):
        try:
            with urllib.request.urlopen(request) as response:  # noqa: S310 - fixed https base
                payload: object = json.loads(response.read().decode("utf-8"))
        except (urllib.error.URLError, TimeoutError, json.JSONDecodeError, OSError) as error:
            last_error = error
            if attempt + 1 < LIST_ATTEMPTS:
                time.sleep(LIST_BACKOFF_SECONDS * (attempt + 1))
            continue
        if not isinstance(payload, Mapping):
            raise BackfillFailure(f"R2 listing did not return an object: {url}")
        if not payload.get("success", False):
            raise BackfillFailure(f"R2 listing failed: {json.dumps(payload.get('errors'))}")
        return payload
    raise BackfillFailure(f"R2 listing could not be read after {LIST_ATTEMPTS} attempts: {last_error}")


def list_objects(scope: Scope, bucket: str, prefix: str) -> list[Mapping[str, Any]]:
    """Page through one R2 prefix. ``wrangler`` cannot do this; the REST API can."""
    objects: list[Mapping[str, Any]] = []
    cursor: str | None = None
    base = (
        f"{scope.api_base.rstrip('/')}/accounts/{scope.account_id}"
        f"/r2/buckets/{bucket}/objects"
    )
    while True:
        query = {"prefix": prefix, "per_page": str(LIST_PAGE_SIZE)}
        if cursor:
            query["cursor"] = cursor
        payload = _http_json(f"{base}?{urllib.parse.urlencode(query)}", scope.api_token)
        result = payload.get("result")
        if isinstance(result, list):
            objects.extend(item for item in result if isinstance(item, Mapping))
        info_block = payload.get("result_info")
        if not isinstance(info_block, Mapping) or not info_block.get("is_truncated"):
            return objects
        next_cursor = info_block.get("cursor")
        if not isinstance(next_cursor, str) or not next_cursor:
            return objects
        cursor = next_cursor


def _uploaded_on(value: object) -> date | None:
    if not isinstance(value, str):
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00")).date()
    except ValueError:
        return None


def _custom_game_type(value: object) -> str | None:
    if not isinstance(value, Mapping):
        return None
    game_type = value.get("game_type")
    return game_type if isinstance(game_type, str) and game_type else None


def parse_raw_object(item: Mapping[str, Any]) -> RawChunk | None:
    """Turn one listed ``raw/`` object into a chunk, or ``None`` if it is not one."""
    key = item.get("key")
    if not isinstance(key, str):
        return None
    match = RAW_KEY_PATTERN.match(key)
    if match is None:
        return None
    user_id = match.group("user_id")
    game_id = match.group("game_id")
    chunk_id = match.group("chunk_id")
    if (
        re.fullmatch(USER_ID_PATTERN, user_id) is None
        or re.fullmatch(GAME_ID_PATTERN, game_id) is None
        or re.fullmatch(CHUNK_ID_PATTERN, chunk_id) is None
    ):
        return None
    size = item.get("size")
    return RawChunk(
        key=key,
        user_id=user_id,
        game_id=game_id,
        chunk_id=chunk_id,
        size=size if isinstance(size, int) and not isinstance(size, bool) else 0,
        uploaded_on=_uploaded_on(item.get("last_modified")),
        game_type=_custom_game_type(item.get("custom_metadata")),
    )


def group_games(objects: Iterable[Mapping[str, Any]]) -> tuple[list[Game], list[str]]:
    """Collect ``raw/`` objects into games, reporting keys that do not belong."""
    grouped: dict[tuple[str, str], list[RawChunk]] = {}
    rejected: list[str] = []
    for item in objects:
        chunk = parse_raw_object(item)
        if chunk is None:
            key = item.get("key")
            rejected.append(str(key) if isinstance(key, str) else "<object without a key>")
            continue
        grouped.setdefault((chunk.user_id, chunk.game_id), []).append(chunk)

    games = [
        Game(
            user_id=user_id,
            game_id=game_id,
            chunks=tuple(sorted(chunks, key=lambda item: item.key)),
        )
        for (user_id, game_id), chunks in grouped.items()
    ]
    games.sort(key=lambda game: (game.game_id, game.user_id))

    # The Parquet key is keyed on game_id alone, so two users sharing one would
    # overwrite each other. Refuse rather than pick a winner.
    owners: dict[str, set[str]] = {}
    for game in games:
        owners.setdefault(game.game_id, set()).add(game.user_id)
    contested = {game_id for game_id, users in owners.items() if len(users) > 1}
    if contested:
        for game_id in sorted(contested):
            rejected.append(f"{game_id}: raw chunks belong to more than one user")
    return [game for game in games if game.game_id not in contested], rejected


def existing_parquet_game_ids(objects: Iterable[Mapping[str, Any]]) -> dict[str, str]:
    """Map every already-written episode's game_id to its Parquet key."""
    written: dict[str, str] = {}
    for item in objects:
        key = item.get("key")
        if not isinstance(key, str):
            continue
        match = PARQUET_KEY_PATTERN.match(key)
        if match is not None:
            written[match.group("game_id")] = key
    return written


def fetch_manifest(bucket: str, manifest_key: str, scope: Scope) -> Mapping[str, Any] | None:
    """Read one manifest sidecar out of R2, or ``None`` if it is unusable."""
    with tempfile.TemporaryDirectory(prefix=".backfill-manifest-") as directory:
        destination = Path(directory) / "manifest.json"
        if get_object(bucket, manifest_key, destination, scope) is not None:
            return None
        try:
            payload: object = json.loads(destination.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return None
    return payload if isinstance(payload, Mapping) else None


def inventory_digest(inventory: Iterable[Mapping[str, Any]], key: str) -> str | None:
    """``payload_sha256`` from R2 custom metadata, when the listing carries it.

    ``EpisodeConsumer`` stamps it, but ``wrangler r2 object put`` cannot set
    custom metadata, so anything this script wrote carries the digest only in
    its manifest sidecar. Callers fall back to that — the same order
    ``download-r2-dataset.py`` already trusts.
    """
    for item in inventory:
        if item.get("key") != key:
            continue
        metadata = item.get("custom_metadata")
        if isinstance(metadata, Mapping):
            stored = metadata.get("payload_sha256")
            if isinstance(stored, str) and SHA256_PATTERN.fullmatch(stored):
                return stored
        return None
    return None


def comparable_manifest(manifest: Mapping[str, Any]) -> dict[str, Any]:
    """The manifest minus the one field that legitimately changes every run."""
    return {key: value for key, value in manifest.items() if key != "written_at_ms"}


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


def put_object(bucket: str, key: str, source: Path, content_type: str, scope: Scope) -> None:
    args = [
        "r2",
        "object",
        "put",
        f"{bucket}/{key}",
        "--file",
        str(source),
        "--content-type",
        content_type,
        *scope.r2_flags(),
    ]
    completed = wrangler(*args)
    if completed.returncode != 0:
        detail = completed.stderr.strip() or completed.stdout.strip()
        raise GameFailure(f"could not write {key}: {detail or completed.returncode}")


def read_chunk(bucket: str, chunk: RawChunk, scope: Scope) -> bytes:
    with tempfile.TemporaryDirectory(prefix=".backfill-chunk-") as directory:
        destination = Path(directory) / "chunk.json"
        error = get_object(bucket, chunk.key, destination, scope)
        if error is not None:
            raise GameFailure(f"could not read {chunk.key}: {error}")
        return destination.read_bytes()


def replay_assembly(
    envelopes: Sequence[Any],
    *,
    max_chunks: int,
) -> EpisodeReady:
    """Re-run the Durable Object's state machine over one game's chunks.

    Identical to what the live assembler would have decided, including the
    ``complete``/``incomplete``/``abandoned`` classification, because it is the
    same :class:`EpisodeState`.
    """
    state = EpisodeState()
    ordered = sorted(envelopes, key=lambda envelope: envelope.chunk_sequence)
    for envelope in ordered:
        ended = any(event.get("event_type") == "episode_ended" for event in envelope.events)
        try:
            episode = state.record(
                EpisodeChunkRecord(
                    chunk_id=envelope.chunk_id,
                    chunk_sequence=envelope.chunk_sequence,
                    first_event_sequence=envelope.first_event_sequence,
                    last_event_sequence=envelope.last_event_sequence,
                    created_at_ms=envelope.created_at_ms,
                    episode_ended=ended,
                ),
                user_id=envelope.user_id,
                game_id=envelope.game_id,
                game_type=envelope.game_type,
                game_version=envelope.game_version,
                now_ms=envelope.created_at_ms,
                idle_ttl_ms=REPLAY_IDLE_TTL_MS,
                max_chunks=max_chunks,
            )
        except ValueError as error:
            raise GameFailure(f"chunks do not form one episode: {error}") from error
        if episode is not None:
            return episode

    # No chunk closed the episode, so it is whatever the idle alarm would have
    # flushed it as: `incomplete` if episode_ended arrived without a clean
    # chain, `abandoned` if the player simply walked away.
    last = ordered[-1]
    episode = state.flush(now_ms=last.created_at_ms + REPLAY_IDLE_TTL_MS)
    if episode is None:
        raise GameFailure("assembly produced no episode")
    return episode


def convert_game(
    game: Game,
    *,
    bucket: str,
    scope: Scope,
    game_types: frozenset[str] | None,
    max_chunks: int,
) -> Conversion | None:
    """Read every raw chunk and derive the episode. ``None`` means filtered out."""
    envelopes: list[Any] = []
    chunk_bodies: list[dict[str, object]] = []
    for chunk in game.chunks:
        body = read_chunk(bucket, chunk, scope)
        try:
            envelope, payload = parse_gameplay_chunk(body, chunk.user_id)
        except PermanentGameplayError as error:
            raise GameFailure(f"{chunk.chunk_id} is not a valid v2 chunk: {error.reason}") from error
        if envelope.game_id != chunk.game_id or envelope.chunk_id != chunk.chunk_id:
            raise GameFailure(f"{chunk.key} holds a different chunk identity")
        # The oldest raw objects predate `game_type` in custom metadata, so the
        # filter is only decidable once the body has been read.
        if game_types is not None and envelope.game_type not in game_types:
            return None
        envelopes.append(envelope)
        chunk_bodies.append(payload)

    episode = replay_assembly(envelopes, max_chunks=max_chunks)
    kept = {chunk.chunk_id for chunk in episode.chunks}
    try:
        rows = build_rows(
            [
                payload
                for envelope, payload in zip(envelopes, chunk_bodies, strict=True)
                if envelope.chunk_id in kept
            ],
            source_chunk_ids=[chunk.chunk_id for chunk in episode.chunks],
        )
    except EpisodeConversionError as error:
        raise GameFailure(f"chunks do not satisfy the BC join: {error}") from error

    try:
        parquet_key = episode_parquet_key(
            game_type=episode.game_type,
            game_id=episode.game_id,
            completed_at_ms=episode.completed_at_ms,
        )
    except (ValueError, OSError, OverflowError) as error:
        raise GameFailure(f"episode identity is unroutable: {error}") from error

    parquet_body = write_parquet(EPISODE_COLUMNS, rows.parquet_rows)
    return Conversion(
        episode=episode,
        episode_id=episode_id_for(episode.user_id, episode.game_id),
        parquet_key=parquet_key,
        manifest_key=episode_manifest_key(parquet_key),
        parquet_body=parquet_body,
        parquet_sha256=hashlib.sha256(parquet_body).hexdigest(),
        rows=rows,
    )


def upsert_sql(conversion: Conversion, written_at_ms: int) -> str:
    """Render the same upsert ``D1Repository.upsert_episode`` runs, with literals."""
    episode = conversion.episode
    values: tuple[object, ...] = (
        conversion.episode_id,
        episode.user_id,
        episode.game_id,
        episode.game_type,
        episode.game_version,
        episode.status,
        episode.chunk_count,
        len(conversion.rows.rows),
        episode.first_event_sequence,
        episode.last_event_sequence,
        episode.completed_at_ms,
        conversion.parquet_key,
        len(conversion.parquet_body),
        conversion.parquet_sha256,
        conversion.manifest_key,
        written_at_ms,
    )
    rendered = ", ".join(
        sql_quote(value) if isinstance(value, str) else str(value)
        for value in values
    )
    assignments = ",\n    ".join(
        f"{column} = excluded.{column}" for column in UPSERT_EPISODE_COLUMNS[1:]
    )
    columns = ", ".join(UPSERT_EPISODE_COLUMNS)
    return (
        f"INSERT INTO episodes ({columns})\nVALUES ({rendered})\n"
        f"ON CONFLICT(episode_id) DO UPDATE SET\n    {assignments};"
    )


def index_episode(conversion: Conversion, written_at_ms: int, *, database: str, scope: Scope) -> None:
    statement = upsert_sql(conversion, written_at_ms)
    completed = wrangler(
        "d1", "execute", database, "--command", statement, *scope.d1_flags()
    )
    if completed.returncode != 0:
        detail = completed.stderr.strip() or completed.stdout.strip()
        hint = "migrations/0003_episodes_index.sql may not be applied (run `make migrate-remote`)"
        raise GameFailure(f"D1 upsert failed; {hint}: {detail or completed.returncode}")


def write_conversion(
    conversion: Conversion,
    *,
    bucket: str,
    database: str,
    scope: Scope,
    skip_index: bool,
    clock_ms: int,
    write_parquet_object: bool = True,
    write_manifest_object: bool = True,
) -> None:
    """R2 first, then D1, so the index never names an object that is absent."""
    manifest = conversion.manifest(written_at_ms=clock_ms)
    with tempfile.TemporaryDirectory(prefix=".backfill-write-") as directory:
        staging = Path(directory)
        if write_parquet_object:
            parquet_path = staging / "episode.parquet"
            parquet_path.write_bytes(conversion.parquet_body)
            put_object(
                bucket,
                conversion.parquet_key,
                parquet_path,
                "application/vnd.apache.parquet",
                scope,
            )
        if write_manifest_object:
            manifest_path = staging / "episode.manifest.json"
            manifest_path.write_text(canonical_json(manifest), encoding="utf-8")
            put_object(
                bucket, conversion.manifest_key, manifest_path, "application/json", scope
            )
    if not skip_index:
        index_episode(conversion, clock_ms, database=database, scope=scope)


def select_games(
    games: Sequence[Game],
    *,
    written: Mapping[str, str],
    state: State,
    game_ids: frozenset[str] | None,
    since: date | None,
    until: date | None,
    force: bool,
    limit: int | None,
    summary: Summary,
) -> list[Game]:
    """Apply every filter that does not require reading an object's body."""
    selected: list[Game] = []
    for game in games:
        if game_ids is not None and game.game_id not in game_ids:
            summary.filtered += 1
            continue
        days = game.upload_days
        if since is not None and not any(day >= since for day in days):
            summary.filtered += 1
            continue
        if until is not None and not any(day <= until for day in days):
            summary.filtered += 1
            continue
        if not force and game.game_id in written:
            summary.skipped += 1
            continue
        if not force and game.game_id in state.completed:
            summary.skipped += 1
            continue
        selected.append(game)
        if limit is not None and len(selected) >= limit:
            break
    return selected


def process_game(
    game: Game,
    *,
    bucket: str,
    database: str,
    scope: Scope,
    game_types: frozenset[str] | None,
    max_chunks: int,
    dry_run: bool,
    skip_index: bool,
    written: Mapping[str, str],
    inventory: Sequence[Mapping[str, Any]],
    state: State,
    summary: Summary,
) -> None:
    label = game.label
    # When R2 custom metadata already names the game type, a dry run can decide
    # the filter from the listing alone and never read the objects.
    if (
        dry_run
        and game_types is not None
        and game.declared_game_type is not None
        and game.declared_game_type not in game_types
    ):
        summary.filtered += 1
        return

    try:
        conversion = convert_game(
            game,
            bucket=bucket,
            scope=scope,
            game_types=game_types,
            max_chunks=max_chunks,
        )
    except GameFailure as error:
        summary.failed += 1
        fail(f"{label}: {error}")
        return
    if conversion is None:
        summary.filtered += 1
        return

    detail = (
        f"{conversion.episode.status}, {conversion.episode.chunk_count} chunk(s), "
        f"{len(conversion.rows.rows)} row(s), {len(conversion.parquet_body)} bytes"
    )

    # A game that already has Parquet is only reachable under --force. Compare
    # the two artifacts separately: the Parquet bytes of an episode whose only
    # defect is its recorded status are not wrong, and rewriting them would
    # churn `parquet_sha256` for no reason.
    write_parquet_object = True
    write_manifest_object = True
    if game.game_id in written:
        existing_key = written[game.game_id]
        if existing_key != conversion.parquet_key:
            warn(
                f"{label}: re-derived key {conversion.parquet_key} differs from"
                + f" the existing {existing_key}; the old object is left in place"
            )
        else:
            write_parquet_object, write_manifest_object = _plan_writes(
                conversion,
                existing_key=existing_key,
                bucket=bucket,
                scope=scope,
                inventory=inventory,
            )
        if not write_parquet_object and not write_manifest_object:
            # Nothing in R2 changes. The D1 upsert still runs below, because the
            # index may be missing rows the artifacts already describe.
            detail = f"{detail}; R2 unchanged"
        elif not write_parquet_object:
            detail = f"{detail}; manifest only, parquet_sha256 preserved"

    if dry_run:
        summary.converted += 1
        summary.total_rows += len(conversion.rows.rows)
        summary.total_bytes += len(conversion.parquet_body)
        info(f"{label}: would write {conversion.parquet_key} ({detail})")
        return

    if game.game_id in written and not write_parquet_object and not write_manifest_object and skip_index:
        summary.skipped += 1
        ok(f"{label}: already written with identical artifacts")
        return

    try:
        write_conversion(
            conversion,
            bucket=bucket,
            database=database,
            scope=scope,
            skip_index=skip_index,
            clock_ms=time.time_ns() // 1_000_000,
            write_parquet_object=write_parquet_object,
            write_manifest_object=write_manifest_object,
        )
    except GameFailure as error:
        summary.failed += 1
        fail(f"{label}: {error}")
        return

    state.record(game.game_id, conversion.parquet_key)
    summary.converted += 1
    summary.total_rows += len(conversion.rows.rows)
    summary.total_bytes += len(conversion.parquet_body)
    ok(f"{label}: wrote {conversion.parquet_key} ({detail})")


def _plan_writes(
    conversion: Conversion,
    *,
    existing_key: str,
    bucket: str,
    scope: Scope,
    inventory: Sequence[Mapping[str, Any]],
) -> tuple[bool, bool]:
    """Decide which of an already-converted episode's two objects must change."""
    existing_manifest = fetch_manifest(bucket, conversion.manifest_key, scope)
    digest = inventory_digest(inventory, existing_key)
    if digest is None and existing_manifest is not None:
        stored = existing_manifest.get("parquet_sha256")
        digest = stored if isinstance(stored, str) else None
    manifest_changed = existing_manifest is None or comparable_manifest(
        existing_manifest
    ) != comparable_manifest(conversion.manifest(written_at_ms=0))
    return digest != conversion.parquet_sha256, manifest_changed


def print_summary(summary: Summary, *, dry_run: bool) -> None:
    converted = "episodes to write" if dry_run else "episodes written"
    print("\nSummary", flush=True)
    print(f"  {converted}: {summary.converted}", flush=True)
    print(f"  games already converted: {summary.skipped}", flush=True)
    print(f"  games out of scope: {summary.filtered}", flush=True)
    print(f"  games failed: {summary.failed}", flush=True)
    print(f"  parquet rows: {summary.total_rows}", flush=True)
    print(f"  parquet bytes: {summary.total_bytes}", flush=True)


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


def resolve_game_ids(values: Sequence[str] | None) -> frozenset[str] | None:
    if not values:
        return None
    selected = {value.strip() for value in values}
    invalid = sorted(value for value in selected if re.fullmatch(GAME_ID_PATTERN, value) is None)
    if invalid:
        raise argparse.ArgumentTypeError(f"--game-id is not a valid game id: {invalid[0]!r}")
    return frozenset(selected)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Backfill episode Parquet objects from gameplay chunks stranded in R2 raw/.",
    )
    parser.add_argument(
        "--game-type",
        action="append",
        required=True,
        metavar="TYPE",
        help="Game type to convert; repeat for several, or pass `all`.",
    )
    parser.add_argument(
        "--game-id",
        action="append",
        default=None,
        metavar="GAME_ID",
        help="Convert only these game ids; repeat for several.",
    )
    parser.add_argument(
        "--since",
        type=parse_date,
        default=None,
        metavar="YYYY-MM-DD",
        help="Only games with a raw chunk uploaded on or after this UTC date.",
    )
    parser.add_argument(
        "--until",
        type=parse_date,
        default=None,
        metavar="YYYY-MM-DD",
        help="Only games with a raw chunk uploaded on or before this UTC date.",
    )
    parser.add_argument("--bucket", default=DEFAULT_BUCKET)
    parser.add_argument("--database", default=DEFAULT_DATABASE)
    parser.add_argument(
        "--raw-prefix",
        default=DEFAULT_RAW_PREFIX,
        help="R2 key prefix the ingest API writes chunks to.",
    )
    parser.add_argument("--limit", type=positive_int, default=None)
    parser.add_argument(
        "--max-chunks",
        type=positive_int,
        default=DEFAULT_EPISODE_MAX_CHUNKS,
        help="Assembly's per-episode chunk ceiling; match EPISODE_MAX_CHUNKS.",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Report what would be converted without writing to R2 or D1.",
    )
    parser.add_argument(
        "--force",
        action="store_true",
        help=(
            "Also re-derive games that already have Parquet. Each artifact is"
            + " compared before it is written: unchanged Parquet keeps its bytes"
            + " and its parquet_sha256, and only a manifest whose contents"
            + " actually changed is rewritten. Use this to correct the recorded"
            + " status of already-converted episodes."
        ),
    )
    parser.add_argument(
        "--skip-index",
        action="store_true",
        help="Write the R2 artifacts but not the D1 `episodes` row.",
    )
    parser.add_argument(
        "--state-file",
        type=Path,
        default=Path(DEFAULT_STATE_FILE),
        help="Resume ledger of finished games.",
    )
    parser.add_argument(
        "--config",
        default=DEFAULT_REMOTE_CONFIG,
        help="Wrangler config used to resolve remote D1.",
    )
    parser.add_argument(
        "--api-base",
        default=DEFAULT_API_BASE,
        help="Cloudflare REST API base used for the R2 listing.",
    )
    parser.add_argument(
        "--account-id",
        default=None,
        help="Cloudflare account id; defaults to $CLOUDFLARE_ACCOUNT_ID.",
    )
    return parser


def resolve_scope(arguments: argparse.Namespace, parser: argparse.ArgumentParser) -> Scope:
    account_id = arguments.account_id or os.environ.get("CLOUDFLARE_ACCOUNT_ID", "")
    if not account_id:
        parser.error("set CLOUDFLARE_ACCOUNT_ID or pass --account-id")
    api_token = os.environ.get("CLOUDFLARE_API_TOKEN", "")
    if not api_token:
        parser.error("set CLOUDFLARE_API_TOKEN; the R2 listing needs it")
    return Scope(
        account_id=account_id,
        api_token=api_token,
        api_base=arguments.api_base,
        config=arguments.config,
    )


def run(arguments: argparse.Namespace, parser: argparse.ArgumentParser) -> int:
    try:
        game_types = resolve_game_types(arguments.game_type)
        game_ids = resolve_game_ids(arguments.game_id)
    except argparse.ArgumentTypeError as error:
        parser.error(str(error))
    since: date | None = arguments.since
    until: date | None = arguments.until
    if since is not None and until is not None and since > until:
        parser.error(f"--since {since.isoformat()} is after --until {until.isoformat()}")

    scope = resolve_scope(arguments, parser)
    selection = "all game types" if game_types is None else ", ".join(sorted(game_types))
    window = f"{since or 'beginning'} .. {until or 'now'}"
    print(f"Backfill: {selection}   uploaded: {window}   bucket: {arguments.bucket}")

    raw_objects = list_objects(scope, arguments.bucket, arguments.raw_prefix)
    inventory = list_objects(scope, arguments.bucket, f"{EPISODE_PARQUET_PREFIX}/")
    games, rejected = group_games(raw_objects)
    written = existing_parquet_game_ids(inventory)

    summary = Summary()
    for reason in rejected:
        summary.failed += 1
        fail(f"unusable raw object: {reason}")

    state = State.load(arguments.state_file)
    selected = select_games(
        games,
        written=written,
        state=state,
        game_ids=game_ids,
        since=since,
        until=until,
        force=arguments.force,
        limit=arguments.limit,
        summary=summary,
    )
    print(
        f"{len(games)} game(s) in raw/, {len(written)} already have Parquet,"
        + f" {len(selected)} selected."
    )

    for game in selected:
        process_game(
            game,
            bucket=arguments.bucket,
            database=arguments.database,
            scope=scope,
            game_types=game_types,
            max_chunks=arguments.max_chunks,
            dry_run=arguments.dry_run,
            skip_index=arguments.skip_index,
            written=written,
            inventory=inventory,
            state=state,
            summary=summary,
        )

    print_summary(summary, dry_run=arguments.dry_run)
    return 1 if summary.failed else 0


def main(argv: Sequence[str] | None = None) -> int:
    parser = build_parser()
    arguments = parser.parse_args(argv)
    try:
        return run(arguments, parser)
    except BackfillFailure as error:
        print(f"\n\033[31mFAIL\033[0m {error}", file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        # The ledger is saved after every game, so the next run resumes here.
        print("\ninterrupted; re-run to resume", file=sys.stderr)
        return 130


if __name__ == "__main__":
    raise SystemExit(main())
