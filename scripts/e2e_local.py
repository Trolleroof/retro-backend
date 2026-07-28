"""Prove the sync contract end to end, failing loudly on any mismatch.

Walks the path a real client takes and asserts every hop:

1. the Worker is live and its D1 binding is ready
2. an opaque bearer token resolves to the canonical user id
3. ``GET /v1/me`` returns exactly that id (the only identity clients may record)
4. a v2 chunk carrying that id is accepted (202) with a well-formed ack
5. the raw object lands in R2 under ``raw/user=.../game=.../chunk=....json``
6. the queue consumer derives ``validated/date=.../user=.../...json``
7. a game result is accepted (200) and lands in the D1 ``high_scores`` table
8. a chunk carrying the client's *current* identity derivation is rejected (400)

Step 8 is the regression guard for the identity blocker: the tui-gamepigeon
client derives ``usr_<better-auth-id>`` or a random ``usr_<uuid>``, neither of
which can equal ``usr_`` + ``sha256(auth_user_id)[:32]``. Until the client reads
its id from ``/v1/me``, every real upload 400s.

Run against ``make dev`` (see docs/e2e-local.md), or against the deployed
Worker with ``--remote --base-url https://api.runretroarcade.com``. Remote runs
touch production D1 and R2, so every row and object they create is removed
again before the run exits.
"""

import argparse
import hashlib
import json
import re
import secrets
import subprocess
import sys
import time
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from uuid import uuid4

import httpx

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from retro_backend.auth import canonical_user_id, hash_token  # noqa: E402

FIXTURE = ROOT / "contracts/fixtures/interaction-trace-chunk-v2.json"
DEFAULT_BASE_URL = "http://127.0.0.1:8787"
DEFAULT_DATABASE = "retro-backend"
DEFAULT_BUCKET = "retro-gameplay-raw"
DEFAULT_AUTH_USER_ID = "e2e-local-auth-user"
DEFAULT_REMOTE_CONFIG = "wrangler.production.toml"
WRANGLER_VERSION = "4.112.0"


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


class E2EFailure(RuntimeError):
    """A contract violation that must fail the run."""


_step_number = 0


def step(title: str) -> None:
    global _step_number
    _step_number += 1
    print(f"\n\033[1m[{_step_number}] {title}\033[0m", flush=True)


def ok(message: str) -> None:
    print(f"    \033[32m✓\033[0m {message}", flush=True)


def fail(message: str) -> None:
    print(f"    \033[31m✗\033[0m {message}", flush=True)


def check(condition: bool, message: str) -> None:
    if not condition:
        raise E2EFailure(message)
    ok(message)


def wrangler(*args: str, capture: bool = True) -> subprocess.CompletedProcess[str]:
    command = ["npx", "--yes", f"wrangler@{WRANGLER_VERSION}", *args]
    return subprocess.run(
        command,
        cwd=ROOT,
        capture_output=capture,
        text=True,
        check=False,
    )


def d1_execute(database: str, sql: str, scope: Scope) -> list[dict[str, Any]]:
    args = ["d1", "execute", database, "--json", "--command", sql, *scope.d1_flags()]
    completed = wrangler(*args)
    if completed.returncode != 0:
        detail = completed.stderr.strip() or completed.stdout.strip()
        raise E2EFailure(
            f"wrangler d1 execute failed ({completed.returncode}):\n{detail}"
        )
    # Wrangler prints human-readable preamble lines before the JSON payload.
    text = completed.stdout
    start = text.find("[")
    if start == -1:
        return []
    try:
        payload = json.loads(text[start:])
    except json.JSONDecodeError as error:
        raise E2EFailure(f"could not parse d1 --json output:\n{text}") from error
    results: list[dict[str, Any]] = []
    for block in payload if isinstance(payload, list) else []:
        rows = block.get("results") if isinstance(block, dict) else None
        if isinstance(rows, list):
            results.extend(row for row in rows if isinstance(row, dict))
    return results


def r2_object_exists(bucket: str, key: str, scope: Scope) -> bool:
    args = ["r2", "object", "get", f"{bucket}/{key}", "--pipe", *scope.r2_flags()]
    completed = wrangler(*args)
    return completed.returncode == 0


def r2_object_delete(bucket: str, key: str, scope: Scope) -> bool:
    args = ["r2", "object", "delete", f"{bucket}/{key}", *scope.r2_flags()]
    return wrangler(*args).returncode == 0


def client_normalize_account_id(value: str) -> str:
    """Mirror ``normalizeAccountId`` from the client's ``recording/identity.ts``.

    Reproduced exactly, hyphens included: a Better Auth id matching
    ``[A-Za-z0-9._-]+`` takes the prefix branch, so the hash branch is
    unreachable for real ids. Keep this faithful to the client, or the guard
    below can pass while the derivation it claims to cover goes unchecked.
    """
    if value.startswith("usr_") and re.fullmatch(r"usr_[A-Za-z0-9._-]+", value):
        return value
    if re.fullmatch(r"[A-Za-z0-9._-]+", value):
        return f"usr_{value}"
    digest = hashlib.sha256(value.encode("utf-8")).hexdigest()[:32]
    return f"usr_{digest}"


def sql_quote(value: str) -> str:
    return "'" + value.replace("'", "''") + "'"


def utc_date(created_at_ms: int) -> str:
    return datetime.fromtimestamp(created_at_ms / 1000, tz=UTC).strftime("%Y-%m-%d")


def build_chunk(user_id: str) -> dict[str, Any]:
    """Rewrite the v2 fixture onto a fresh identity so each run is not a duplicate."""
    payload: dict[str, Any] = json.loads(FIXTURE.read_text())
    game_id = f"game_{uuid4()}"
    chunk_id = f"chk_{uuid4()}"
    payload["user_id"] = user_id
    payload["game_id"] = game_id
    payload["chunk_id"] = chunk_id
    for event in payload["events"]:
        event["user_id"] = user_id
        event["game_id"] = game_id
        event["chunk_id"] = chunk_id
        event["event_id"] = f"evt_{uuid4()}"
    return payload


def post(
    client: httpx.Client,
    path: str,
    token: str,
    body: dict[str, Any],
) -> httpx.Response:
    return client.post(
        path,
        content=json.dumps(body).encode("utf-8"),
        headers={
            "Authorization": f"Bearer {token}",
            "Content-Type": "application/json",
        },
    )


def cleanup(
    database: str,
    bucket: str,
    scope: Scope,
    user_id: str,
    object_keys: list[str],
    token_digests: list[str],
) -> list[str]:
    """Remove everything a remote run wrote, and report whatever survived."""
    step("Clean up the artifacts this run created")
    survivors: list[str] = []
    for key in object_keys:
        if r2_object_delete(bucket, key, scope):
            ok(f"deleted R2 {key}")
        else:
            survivors.append(f"R2 {bucket}/{key}")
            fail(f"could not delete R2 {key}")

    quoted_digests = ", ".join(sql_quote(digest) for digest in token_digests)
    statements = [
        f"DELETE FROM high_scores WHERE user_id = {sql_quote(user_id)};",
        f"DELETE FROM auth_sessions WHERE user_id = {sql_quote(user_id)};",
        # Tombstones are keyed by digest alone, so they outlive the user row.
        f"DELETE FROM revoked_tokens WHERE token_hash IN ({quoted_digests});",
        f"DELETE FROM users WHERE id = {sql_quote(user_id)};",
    ]
    try:
        d1_execute(database, " ".join(statements), scope)
    except E2EFailure as error:
        survivors.append(f"D1 rows for {user_id}")
        fail(f"could not delete D1 rows for {user_id}: {error}")
    else:
        ok(f"deleted every D1 row for {user_id}")
    return survivors


def run(arguments: argparse.Namespace) -> None:
    base_url: str = arguments.base_url.rstrip("/")
    database: str = arguments.database
    bucket: str = arguments.bucket
    auth_user_id: str = arguments.auth_user_id
    scope = Scope(
        remote=arguments.remote,
        persist_to=arguments.persist_to,
        config=arguments.config,
    )
    user_id = canonical_user_id(auth_user_id)
    object_keys: list[str] = []
    token_digests: list[str] = []

    if not scope.remote:
        walk(
            arguments, scope, base_url, database, bucket,
            user_id, object_keys, token_digests,
        )
        return

    try:
        walk(
            arguments, scope, base_url, database, bucket,
            user_id, object_keys, token_digests,
        )
    except BaseException:
        # The walk already failed; still try to clear production, but let the
        # original contract failure be the one that surfaces.
        cleanup(database, bucket, scope, user_id, object_keys, token_digests)
        raise
    survivors = cleanup(database, bucket, scope, user_id, object_keys, token_digests)
    if survivors:
        raise E2EFailure(
            "the contract held, but this run left test state in production:\n  "
            + "\n  ".join(survivors)
            + "\ndelete it by hand before treating this run as clean"
        )


def walk(
    arguments: argparse.Namespace,
    scope: Scope,
    base_url: str,
    database: str,
    bucket: str,
    user_id: str,
    object_keys: list[str],
    token_digests: list[str],
) -> None:
    auth_user_id: str = arguments.auth_user_id

    with httpx.Client(base_url=base_url, timeout=15) as client:
        step(f"Worker liveness and readiness at {base_url}")
        try:
            live = client.get("/health/live")
        except httpx.HTTPError as error:
            hint = (
                "check the deployment"
                if scope.remote
                else "start it with `make dev`"
            )
            raise E2EFailure(
                f"cannot reach the Worker at {base_url}; {hint}\n  {error}"
            ) from error
        check(live.status_code == 200, f"GET /health/live -> {live.status_code}")
        ready = client.get("/health/ready")
        check(
            ready.status_code == 200,
            f"GET /health/ready -> {ready.status_code} (D1 binding present)",
        )

        step("Provision an opaque bearer token for the canonical user")
        token = secrets.token_urlsafe(32)
        digest = hash_token(token)
        token_digests.append(digest)
        print(f"    auth_user_id   {auth_user_id}")
        print(f"    canonical id   {user_id}")
        d1_execute(
            database,
            "INSERT INTO users (id, auth_token_hash) VALUES "
            + f"({sql_quote(user_id)}, {sql_quote(digest)}) "
            + "ON CONFLICT(id) DO UPDATE SET "
            + "auth_token_hash = excluded.auth_token_hash; "
            + "INSERT INTO auth_sessions (token_hash, user_id) VALUES "
            + f"({sql_quote(digest)}, {sql_quote(user_id)}) "
            + "ON CONFLICT(token_hash) DO UPDATE SET "
            + "user_id = excluded.user_id;",
            scope,
        )
        where = "remote" if scope.remote else "local"
        ok(f"users and auth_sessions rows upserted in {where} D1")

        step("GET /v1/me returns the identity clients must record with")
        me = client.get("/v1/me", headers={"Authorization": f"Bearer {token}"})
        check(me.status_code == 200, f"GET /v1/me -> {me.status_code}")
        check(
            me.json() == {"user_id": user_id},
            f"/v1/me user_id == {user_id}",
        )

        step("POST a v2 interaction-trace chunk")
        chunk = build_chunk(user_id)
        response = post(client, "/v1/game-event-chunks", token, chunk)
        if response.status_code != 202:
            raise E2EFailure(
                "expected 202 from /v1/game-event-chunks, "
                + f"got {response.status_code}: {response.text}"
            )
        ok("POST /v1/game-event-chunks -> 202")
        ack = response.json()
        for field, expected in (
            ("accepted", True),
            ("queued", True),
            ("game_id", chunk["game_id"]),
            ("chunk_id", chunk["chunk_id"]),
            ("chunk_sequence", chunk["chunk_sequence"]),
        ):
            check(ack.get(field) == expected, f"ack.{field} == {expected!r}")
        check(
            ack.get("accepted_event_range")
            == {
                "first": chunk["first_event_sequence"],
                "last": chunk["last_event_sequence"],
            },
            "ack.accepted_event_range matches the submitted range",
        )
        check(
            isinstance(ack.get("server_received_at_ms"), int),
            "ack.server_received_at_ms is an integer",
        )

        step("Raw object landed in R2")
        raw_key = (
            f"raw/user={user_id}/game={chunk['game_id']}"
            f"/chunk={chunk['chunk_id']}.json"
        )
        object_keys.append(raw_key)
        check(
            r2_object_exists(bucket, raw_key, scope),
            f"R2 object present at {raw_key}",
        )

        step("Queue consumer derived a validated artifact")
        validated_key = (
            f"validated/date={utc_date(chunk['created_at_ms'])}/user={user_id}"
            f"/game={chunk['game_id']}/chunk={chunk['chunk_id']}.json"
        )
        object_keys.append(validated_key)
        deadline = time.monotonic() + arguments.queue_timeout
        while True:
            if r2_object_exists(bucket, validated_key, scope):
                ok(f"derived artifact present at {validated_key}")
                break
            if time.monotonic() >= deadline:
                raise E2EFailure(
                    "queue consumer produced no derived artifact within "
                    + f"{arguments.queue_timeout}s at {validated_key}\n"
                    + "  check the `make dev` log for consumer errors"
                )
            time.sleep(1)

        step("POST a game result and confirm the D1 high score")
        score = 4096
        completed_at = datetime.now(tz=UTC).isoformat(timespec="microseconds").replace(
            "+00:00", "Z"
        )
        result_body = {
            "result_id": f"res_{uuid4()}",
            "game_id": chunk["game_id"],
            "game_type": chunk["game_type"],
            "score": score,
            "completed_at": completed_at,
        }
        result = post(client, "/v1/game-results", token, result_body)
        if result.status_code != 200:
            raise E2EFailure(
                "expected 200 from /v1/game-results, "
                + f"got {result.status_code}: {result.text}"
            )
        ok("POST /v1/game-results -> 200")
        check(
            result.json().get("accepted") is True,
            "result body reports accepted == True",
        )
        rows = d1_execute(
            database,
            "SELECT score FROM high_scores WHERE user_id = "
            + f"{sql_quote(user_id)} "
            + f"AND game_type = {sql_quote(chunk['game_type'])};",
            scope,
        )
        check(bool(rows), "high_scores row exists for the canonical user")
        check(
            int(str(rows[0].get("score"))) >= score,
            f"high_scores.score >= {score}",
        )

        step("Regression guard: the client's current identity derivation is rejected")
        for label, wrong_user_id in (
            ("normalizeAccountId prefix", client_normalize_account_id(auth_user_id)),
            ("local user_id file", f"usr_{uuid4()}"),
        ):
            if wrong_user_id == user_id:
                # Only reachable when --auth-user-id contains characters outside
                # [A-Za-z0-9._-], which sends the client down its sha256 branch
                # and coincidentally lands on the canonical id.
                ok(f"{label} happens to equal the canonical id; nothing to guard")
                continue
            wrong = build_chunk(wrong_user_id)
            rejected = post(client, "/v1/game-event-chunks", token, wrong)
            if rejected.status_code != 400:
                raise E2EFailure(
                    f"{label}: expected 400 for identity {wrong_user_id}, "
                    + f"got {rejected.status_code}: {rejected.text}"
                )
            detail = str(rejected.json().get("detail", ""))
            if user_id not in detail or "/v1/me" not in detail:
                raise E2EFailure(
                    f"{label}: 400 detail must name the expected id and /v1/me, "
                    + f"got: {detail}"
                )
            ok(f"{label} -> 400 naming the expected id")

        step("A second device signs in without evicting the first")
        second_token = secrets.token_urlsafe(32)
        second_digest = hash_token(second_token)
        token_digests.append(second_digest)
        d1_execute(
            database,
            "INSERT INTO auth_sessions (token_hash, user_id) VALUES "
            + f"({sql_quote(second_digest)}, {sql_quote(user_id)}) "
            + "ON CONFLICT(token_hash) DO UPDATE SET user_id = excluded.user_id;",
            scope,
        )
        for label, device_token in (("first", token), ("second", second_token)):
            me = client.get(
                "/v1/me", headers={"Authorization": f"Bearer {device_token}"}
            )
            if me.status_code != 200 or me.json() != {"user_id": user_id}:
                raise E2EFailure(
                    f"{label} device token: expected 200 with {user_id}, "
                    + f"got {me.status_code}: {me.text}"
                )
            ok(f"{label} device token still resolves to {user_id}")

        step("Logout revokes the presented token and leaves the other device alone")
        logout = client.post(
            "/v1/auth/logout", headers={"Authorization": f"Bearer {second_token}"}
        )
        if logout.status_code != 200:
            raise E2EFailure(
                f"expected 200 from /v1/auth/logout, got {logout.status_code}: "
                + logout.text
            )
        check(
            logout.json().get("revoked_sessions") == 1,
            "logout reports exactly one revoked session",
        )
        rows = d1_execute(
            database,
            "SELECT token_hash FROM auth_sessions WHERE user_id = "
            + f"{sql_quote(user_id)};",
            scope,
        )
        check(
            [str(row.get("token_hash")) for row in rows] == [digest],
            "only the first device's session row survives in D1",
        )
        rows = d1_execute(
            database,
            "SELECT token_hash FROM revoked_tokens WHERE token_hash = "
            + f"{sql_quote(second_digest)};",
            scope,
        )
        check(bool(rows), "revoked_tokens holds a tombstone for the logged-out token")

        step("The revoked token is not re-mirrored by the self-heal fallback")
        # The edge cache is not populated for this digest in `wrangler dev`, so
        # the rejection is immediate here; in production a colo that cached the
        # digest keeps accepting it until AUTH_CACHE_TTL_SECONDS elapses.
        revoked_me = client.get(
            "/v1/me", headers={"Authorization": f"Bearer {second_token}"}
        )
        if revoked_me.status_code != 401:
            raise E2EFailure(
                "expected 401 for the revoked token, "
                + f"got {revoked_me.status_code}: {revoked_me.text}"
            )
        ok("GET /v1/me with the revoked token -> 401")
        surviving = client.get("/v1/me", headers={"Authorization": f"Bearer {token}"})
        check(
            surviving.status_code == 200,
            "the first device is still signed in after the other device logged out",
        )


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--base-url", default=DEFAULT_BASE_URL)
    parser.add_argument("--database", default=DEFAULT_DATABASE)
    parser.add_argument("--bucket", default=DEFAULT_BUCKET)
    parser.add_argument(
        "--auth-user-id",
        default=DEFAULT_AUTH_USER_ID,
        help="Upstream auth subject; the Worker hashes it into the canonical id.",
    )
    parser.add_argument(
        "--persist-to",
        default=None,
        help="Local persistence directory, if `wrangler dev` was given one.",
    )
    parser.add_argument(
        "--remote",
        action="store_true",
        help=(
            "Read and write the account's real D1 and R2 instead of the dev "
            + "sim. Everything the run creates is deleted before it exits."
        ),
    )
    parser.add_argument(
        "--config",
        default=DEFAULT_REMOTE_CONFIG,
        help="Wrangler config used to resolve remote D1 (--remote only).",
    )
    parser.add_argument("--queue-timeout", type=float, default=30.0)
    arguments = parser.parse_args()

    try:
        run(arguments)
    except E2EFailure as error:
        print(f"\n\033[31mFAIL\033[0m {error}", file=sys.stderr)
        return 1
    where = "remote" if arguments.remote else "local"
    print(f"\n\033[32mPASS\033[0m {where} end-to-end sync contract verified", flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
