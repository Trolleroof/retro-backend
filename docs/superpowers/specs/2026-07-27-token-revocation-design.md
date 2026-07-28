# Multi-Device Sessions And Token Revocation Design

## Problem

`users.auth_token_hash` stored exactly one token digest per user, and nothing
ever deleted it.

1. **Single device.** A second sign-in overwrote the column, so the first
   device's digest stopped resolving. It appeared to recover only because the
   `AUTH_SESSION_URL` fallback re-mirrored the token on the next request — which
   in turn evicted the second device. Two active devices ping-ponged.
2. **No revocation path.** There was no way to invalidate a token. Deleting the
   row would not have worked either: the same self-heal fallback would have
   re-created it on the next request, because a D1 miss was indistinguishable
   from "not yet mirrored".

## Schema

Migration `0002_auth_sessions.sql` adds:

- `auth_sessions(token_hash PK, user_id, created_at)` — one row per live device
  token, `ON DELETE CASCADE` from `users`.
- `revoked_tokens(token_hash PK, revoked_at)` — tombstones consulted before the
  upstream-session fallback.

Existing `users` rows are backfilled into `auth_sessions`.
`users.auth_token_hash` is retained as a legacy mirror: SQLite cannot drop an
implicitly indexed (`UNIQUE`) column, and rebuilding `users` would fire the
`ON DELETE CASCADE` on `high_scores`. Authentication no longer reads it.

Tombstones are pruned after 30 days on each revocation, which bounds table
growth while outliving any edge-cached copy of the digest.

## Revocation Path

`POST /v1/auth/logout` (`?scope=session|all_devices`, default `session`):

1. Authenticate normally, which yields the caller's `user_id`.
2. Insert the tombstone(s) into `revoked_tokens`, **then** delete the
   `auth_sessions` row(s). The order matters: a concurrent request that misses
   the session row between the two writes must find the tombstone rather than
   re-mirror the token from the auth service.
3. Purge the digest from the edge auth cache, best-effort.
4. Return `revoked_sessions`, `scope`, and `edge_cache_ttl_seconds`.

`all_devices` needs two extra guards, because D1 has no multi-statement
transaction here and each statement lands separately:

- The caller's own digest is tombstoned unconditionally, before the sweep. A
  caller that authenticated from the edge cache may hold no `auth_sessions` row
  at all, so a user-scoped sweep alone would leave its token free to self-heal.
- The sweep tombstones a *snapshot* of the user's digests. A concurrent
  self-heal can insert a row after that snapshot and before the delete, so every
  digest the delete actually returns is tombstoned again afterwards.

A digest inserted after the delete still escapes. That token is one the auth
service considers valid and that was mirrored after logout completed; closing it
needs a real transaction (`D1.batch`), which is the natural follow-up.

`require_user_id` checks `is_token_revoked` before the `AUTH_SESSION_URL`
fallback, costing one extra D1 read only on the miss path. That check is what
makes revocation stick when the upstream auth service still considers the token
valid.

## Residual Window

The Workers Cache API is per-data-center; there is no global purge. Logout
clears the digest in the colo that served the request, so:

- D1 revocation: immediate and authoritative.
- A colo with a warm cache entry: accepts the token until the entry expires,
  bounded by `AUTH_CACHE_TTL_SECONDS` (default 60s, capped at 300s in code).

The logout response reports that bound so clients can surface it honestly rather
than assuming instant invalidation. Closing the window entirely would require a
D1 read on every cache hit, which is exactly the cost the edge cache exists to
avoid.

## Verification

`tests/test_auth_revocation.py` covers the migration shape, multi-device session
inserts, tombstone-before-delete ordering, revocation counting, tombstone
pruning, both logout scopes, scope validation, cache-purge failure, and the
self-heal fallback in both the revoked and unrevoked cases.
`tests/test_auth_cache.py` covers edge and request-local cache purges.
`scripts/e2e_local.py` signs a second device in, logs it out, and asserts the
first device survives while the revoked token gets a `401`.
