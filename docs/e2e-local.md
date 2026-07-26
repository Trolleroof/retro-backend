# Local end-to-end sync runbook

Proves the whole ingestion contract on one machine: **auth → `/v1/me` → chunk →
R2 → queue consumer → derived artifact → D1 high score**, plus a regression
guard for the client identity mismatch described below.

Run this before any deploy. It is the cheapest way to catch identity or schema
drift between this Worker and the `tui-gamepigeon` client.

## Prerequisites

- `uv` and Node (see `.node-version`); everything else is fetched by `npx`.
- No Cloudflare login required — every binding runs in local Miniflare storage.

## Run it

Apply the local D1 schema once (and again after any new migration):

```bash
make migrate-local
```

Start the Worker in one shell and leave it running:

```bash
make dev
```

In a second shell:

```bash
make e2e-local
```

A green `PASS local end-to-end sync contract verified` means every hop holds.
Any failure prints the exact contract that broke and exits non-zero, so this is
safe to wire into CI or a pre-deploy gate.

## What each step asserts

| # | Step | Failure means |
|---|------|---------------|
| 1 | `GET /health/live`, `GET /health/ready` | Worker down, or the D1 binding is missing |
| 2 | Upsert `users` row with the token digest | Local D1 not migrated (`make migrate-local`) |
| 3 | `GET /v1/me` returns the canonical id | Auth resolution or `canonical_user_id` drifted |
| 4 | `POST /v1/game-event-chunks` → `202` + ack | Envelope contract or ack shape drifted |
| 5 | R2 object at `raw/user=…/game=…/chunk=….json` | Ingestion did not persist the raw payload |
| 6 | R2 object at `validated/date=…/user=…/…json` | **Queue consumer is failing** — check the `make dev` log |
| 7 | `POST /v1/game-results` → `200` + `high_scores` row | Result contract or D1 write drifted |
| 8 | A client-derived identity is rejected with `400` | The identity guard regressed |

Step 6 is worth calling out: a broken queue consumer is invisible from the HTTP
response, because ingestion returns `202` as soon as the pointer is enqueued.
Without this check a consumer crash only shows up as silently missing training
data (and, after `max_retries`, DLQ objects).

## Options

```bash
uv run python scripts/e2e_local.py --help
```

- `--base-url` — defaults to `http://127.0.0.1:8787`
- `--auth-user-id` — the upstream auth subject; the Worker hashes it
- `--persist-to` — pass the same directory you gave `wrangler dev`, if any
- `--queue-timeout` — seconds to wait for the derived artifact (default 30)

## The identity rule

The Worker derives the authenticated user from the upstream auth subject:

```
user_id = "usr_" + sha256(auth_user_id).hexdigest()[:32]
```

(`canonical_user_id` in `src/retro_backend/auth.py`.) Chunk ingestion then
requires the envelope's `user_id` **and every event's `user_id`** to equal that
value exactly (`ChunkEnvelope.validate_authenticated_identity`).

**Clients must read their id from `GET /v1/me` and record with it.** No other
derivation works — a client cannot compute this id, because it never sees the
raw `auth_user_id`.

### Known client mismatch

As of this writing `tui-gamepigeon`'s `resolveRecordingUserId()`
(`src/recording/identity.ts`) produces neither:

- With no account, it generates a random `usr_<uuid>` in `~/.tui-gamepigeon/user_id`.
- With an account, `normalizeAccountId()` **prefixes** a Better Auth id
  (`usr_<auth-id>`) rather than hashing it — and it takes the prefix branch for
  any id matching `[A-Za-z0-9._-]+`, which Better Auth ids do.

Both are rejected with `400`. Step 8 of this runbook pins that behaviour, and
`tests/test_identity_contract.py` mirrors the client derivations so CI fails if
either side drifts. The fix is client-side: pass the `/v1/me` id through
`RecordingSyncCoordinator.resolveRecordingIdentity()` into the recorder.

To make the failure self-diagnosing, the `400` names both identities:

```
user_id must match the authenticated user (expected usr_69fc…, received usr_abc…);
use the user_id returned by GET /v1/me
```

## Tokens: opaque digests vs `AUTH_SESSION_URL`

The Worker accepts a bearer token two ways, tried in this order:

1. **Opaque token digest in D1.** `users.auth_token_hash` holds
   `sha256(token)`. `scripts/e2e_local.py` provisions one automatically; for a
   manual token use `scripts/issue_token.py`, which prints the token on stderr
   and the `INSERT` on stdout. Bind `?1` to the **canonical** id — inserting a
   raw Better Auth id here reproduces exactly the mismatch above.

2. **`AUTH_SESSION_URL` fallback.** On a D1 miss the Worker calls the configured
   Better Auth `get-session` endpoint with the same bearer token, hashes the
   returned `user.id` into the canonical id, and caches the pairing back into
   D1. The URL must be HTTPS, except for `127.0.0.1`/`localhost`.

The local runbook uses path 1 so it needs no auth service. To exercise path 2,
point `AUTH_SESSION_URL` at a local stub returning `{"user": {"id": "..."}}` and
skip the D1 insert; `/v1/me` must still return
`usr_ + sha256(that id)[:32]`.

## Client environment

Once the client records with the `/v1/me` identity, point it at this Worker:

```bash
export GAMEPIGEON_BACKEND_URL=http://127.0.0.1:8787
export GAMEPIGEON_BACKEND_TOKEN=<token from step 2 / issue_token.py>
```
