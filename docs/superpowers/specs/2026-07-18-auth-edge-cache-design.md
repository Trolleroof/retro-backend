# Auth Edge Cache Design

## Scope

Add a best-effort Cloudflare Cache API layer to opaque bearer-token resolution.
The cache accelerates repeated authenticated uploads and other protected requests;
it does not cache leaderboard responses, gameplay responses, or files.

## Data Flow

1. Parse `Authorization: Bearer <opaque-token>` using the existing strict parser.
2. Hash the token with SHA-256. Raw bearer tokens never enter cache keys, values,
   logs, D1 queries, R2, Queue messages, or Durable Object state.
3. Check `caches.default` using a synthetic same-origin URL whose path contains a
   cache schema version and the lowercase token digest.
4. On a valid cache hit, return the cached user ID without querying D1.
5. On a miss, malformed entry, or Cache API failure, resolve the digest against
   `users.auth_token_hash` in D1.
6. Cache successful D1 resolutions for 60 seconds. Do not cache invalid tokens.

D1 remains the authoritative and only backing store for auth cache misses. R2
continues to store only raw and derived gameplay files. High scores remain in D1,
and leaderboard responses are not cached.

## Runtime Boundary

The Worker entrypoint wraps the per-request ASGI environment with an auth-cache
adapter backed by `caches.default`. FastAPI reads that adapter from the request's
ASGI environment, preserving local CPython testability and avoiding mutable global
request state.

The cache key uses the incoming request's scheme and authority because Workers
Cache API keys must stay on the Worker's zone. It replaces the external path and
query with an internal versioned auth path, so routes share the same cached token
resolution without including request bodies or bearer headers.

## Security And Failure Behavior

- Cache only positive `digest -> user_id` mappings.
- Use a 60-second TTL, capped in code at five minutes, to bound token-revocation
  delay even if configuration is incorrect.
- Treat cache reads, writes, and malformed values as best-effort failures and
  continue through D1.
- Never return internal cache responses directly to clients.
- Do not use R2, KV, Durable Objects, or in-memory module globals for auth caching.
- Keep authorization failures at `401` with the existing `WWW-Authenticate`
  response.

## Verification

Tests cover cache hits, misses, positive fills, repeated requests, invalid tokens,
malformed entries, Cache API failures, digest-only keys, TTL configuration, and
the absence of leaderboard response caching. Worker validation must also pass
Ruff, BasedPyright, deployment dry-run, and a local runtime smoke test.
