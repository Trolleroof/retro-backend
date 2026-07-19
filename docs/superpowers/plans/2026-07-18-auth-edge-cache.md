# Auth Edge Cache Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Cache successful opaque-token resolutions at the Cloudflare edge while keeping D1 authoritative for every cache miss.

**Architecture:** A focused `EdgeAuthCache` adapter owns digest-only cache keys and versioned JSON values. The FastAPI auth dependency checks an optional per-request cache adapter before D1 and fills it only after a successful D1 lookup; the Worker entrypoint injects `caches.default` without storing request state globally.

**Tech Stack:** Python 3.13, Cloudflare Python Workers, Workers Cache API, FastAPI, D1, pytest

---

### Task 1: Edge Auth Cache Adapter

**Files:**
- Create: `src/retro_backend/auth_cache.py`
- Create: `tests/test_auth_cache.py`

- [ ] **Step 1: Write failing unit tests**

Cover a valid hit, a miss, a digest-only same-origin key, a positive write with
`Cache-Control: max-age=60`, malformed cached JSON, cache read failure, cache write
failure, and validation of lowercase SHA-256 digests.

- [ ] **Step 2: Verify the focused tests fail**

Run: `uv run pytest tests/test_auth_cache.py -q`

Expected: collection fails because `retro_backend.auth_cache` does not exist.

- [ ] **Step 3: Implement the minimal adapter**

Create an `AuthCache` protocol and `EdgeAuthCache` implementation with this public
contract:

```python
class AuthCache(Protocol):
    async def get(self, request_url: str, token_digest: str) -> str | None: ...
    async def put(
        self,
        request_url: str,
        token_digest: str,
        user_id: str,
    ) -> None: ...
```

The concrete adapter receives Cache API, Request-factory, and Response-factory
objects. It stores compact versioned JSON, never a raw token, and treats Cache API
errors as misses or skipped writes.

- [ ] **Step 4: Verify the adapter tests pass**

Run: `uv run pytest tests/test_auth_cache.py -q`

Expected: all auth-cache unit tests pass.

### Task 2: D1-Backed Auth Orchestration

**Files:**
- Modify: `src/retro_backend/auth.py`
- Modify: `src/retro_backend/storage/d1.py`
- Modify: `tests/conftest.py`
- Modify: `tests/test_auth.py`
- Modify: `tests/test_d1.py`
- Modify: `tests/test_results_api.py`

- [ ] **Step 1: Write failing orchestration tests**

Assert that two protected requests with the same valid token perform one D1 auth
lookup, a cache miss performs a D1 lookup and positive fill, invalid tokens are not
cached, cache exceptions fall through to D1, and two leaderboard requests still
perform two leaderboard queries.

- [ ] **Step 2: Verify the focused tests fail**

Run: `uv run pytest tests/test_auth.py tests/test_d1.py tests/test_results_api.py -q`

Expected: the new assertions fail because auth always queries D1.

- [ ] **Step 3: Implement cache-first auth with D1 fallback**

Hash the token once in `require_user_id()`, read `AUTH_CACHE` from the request
environment, and call `D1Repository.resolve_user_hash()` only when the cache has no
valid user. Preserve `resolve_user()` as a convenience wrapper and cache only a
successful D1 result.

- [ ] **Step 4: Verify auth and API tests pass**

Run: `uv run pytest tests/test_auth.py tests/test_d1.py tests/test_results_api.py tests/test_ingestion_api.py -q`

Expected: all focused auth and protected-route tests pass.

### Task 3: Worker Runtime Wiring And Documentation

**Files:**
- Modify: `src/retro_backend/entry.py`
- Modify: `wrangler.toml`
- Modify: `README.md`
- Modify: `docs/superpowers/specs/2026-07-17-cloudflare-python-backend-design.md`
- Modify: `tests/test_entry_contract.py`

- [ ] **Step 1: Write failing Worker contract tests**

Require `caches.default`, a per-request environment wrapper, the non-secret
`AUTH_CACHE_TTL_SECONDS=60` variable, a hard five-minute TTL cap, and documentation
that D1 is the only auth-miss source while leaderboard responses remain uncached.

- [ ] **Step 2: Verify the Worker contract tests fail**

Run: `uv run pytest tests/test_entry_contract.py -q`

Expected: assertions fail because the runtime cache is not wired.

- [ ] **Step 3: Wire the runtime adapter**

Under the Emscripten runtime branch, obtain `caches.default`. Build cache keys with
`workers.Request` and cache values with `workers.Response`, then attach the adapter
to a request-local environment proxy passed to ASGI. Do not change Queue, R2,
Durable Object, rate limiter, result, or leaderboard behavior.

- [ ] **Step 4: Run complete verification**

Run:

```bash
make verify
make worker-sync
make worker-dry-run
```

Expected: all tests, Ruff, BasedPyright, Worker dependency sync, and deployment
dry-run pass with no warnings or uncommitted generated artifacts.

- [ ] **Step 5: Run a local Worker smoke test**

Start `make dev`, seed one local D1 token, request `/v1/me` once, remove the D1 user,
and request `/v1/me` again within 60 seconds. Both responses must return the same
user, proving the second resolution came from Cache API; an uncached token must
still return `401` after the D1 lookup.
