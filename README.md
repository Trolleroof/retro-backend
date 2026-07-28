# Retro Backend

Retro is an all-Python Cloudflare Worker for authenticated gameplay ingestion,
score APIs, 2048/1v1 multiplayer rooms, and asynchronous gameplay artifact
processing. FastAPI handles HTTP through Cloudflare's ASGI adapter; D1, R2,
Queues, Durable Objects, and the native rate limiter are exposed through Worker
bindings.

The production configuration template targets the custom domain
`api.runretroarcade.com`. It is configuration only, not evidence of a live
domain or deployment.

This repository contains the multiplayer backend implementation, but this change
does not deploy it, create remote Durable Object namespaces, run remote migrations,
or publish a release.

The `runretroarcade.com` and `auth.runretroarcade.com` hostnames describe a
possible future website and separately hosted account authentication service.
This change neither creates nor verifies either hostname; this Worker currently
keeps its hash-only opaque-token authentication contract.
## Architecture And Request Flow

The Worker may execute in any Cloudflare location and an isolate may be
restarted at any time. Request state therefore stays local to a handler. Durable
state is kept in bindings:

- D1 is authoritative for users, token hashes, and high scores.
- Cloudflare's Cache API stores positive token-hash-to-user resolutions briefly
  at the edge; every auth cache miss falls through to D1.
- R2 stores exact request bytes and deterministic derived gameplay artifacts.
- `CHUNK_GATE` is one globally addressed Durable Object per logical user/chunk.
- `GAME_ROOMS` creates one SQLite Durable Object per room and owns that room's
  participants, tickets, sockets, boards, actions, and expiry alarm.
- `GAMEPLAY_QUEUE` carries small pointers to R2 objects, never full trajectories.
- The gameplay DLQ is consumed by this Worker and archived under deterministic
  `dlq/` R2 keys before messages are acknowledged.
- `INGEST_RATE_LIMITER` limits authenticated ingestion before storage work.

The Worker fetch boundary checks a valid `Content-Length`, then reads streamed
bodies only up to the route's configured hard cap before the ASGI adapter can
buffer them. It rebuilds the request from those exact bounded bytes without the
original `Content-Length`. The current adapter then makes its own bounded copy
for FastAPI. FastAPI authentication runs after this boundary copy and before
rate limiting, storage, contract validation, or score mutation.

Authentication hashes each bearer token before checking
`caches.default`. A valid hit avoids a D1 auth query. A miss, malformed cache
entry, or Cache API failure queries D1 and caches only a successful resolution
for 60 seconds. When D1 does not recognize a token, the Worker validates it
against `AUTH_SESSION_URL`; a valid Better Auth session is mapped to a stable
`usr_...` identity and its token hash is persisted for later requests. Invalid
tokens are never cached, and raw tokens never enter cache keys or values. Leaderboard responses are not cached. R2 remains limited
to raw and derived gameplay files; it never stores authentication data.

This uses the programmatic Cache API from inside the Worker. Worker-level
caching remains disabled so every request executes the authentication gateway;
do not add a top-level `[cache] enabled = true` setting. Production should use a
Cloudflare custom domain for functional edge-cache operations. Where Cache API
operations are unavailable, authentication safely falls through to D1.

For `POST /v1/game-event-chunks`, the fetch boundary permits at most 256 KiB.
After FastAPI authentication, the route applies the native rate limit, validates
the bounded body, and reserves the logical chunk in `CHUNK_GATE`. It writes the
exact bytes to R2 before publishing a small object pointer to
`retro-gameplay-events`. Only after Queue publication does the Durable Object
persist the accepted acknowledgment. The consumer loads the exact object from
R2, validates it again, and writes deterministic derived objects before
acknowledging the Queue message.

After five exhausted gameplay-processing retries, Cloudflare moves the pointer
to `retro-gameplay-events-dlq`. Its active consumer persists an allowlisted,
token-free archive record to R2 and acknowledges only after that write succeeds.
Ten failed archival attempts move the message to
`retro-gameplay-events-archive-dlq`, which is intentionally unconsumed and must
be monitored as the operator intervention queue.

For `POST /v1/game-results` and any other body-bearing path, the fetch boundary
permits at most 16 KiB. The authenticated result route then validates the JSON
contract and conditionally updates the D1 high score. Rejected bodies never
reach the score mutation.

A successful score upsert returns `200 OK` because the D1 write has already
completed when the response is written; there is no deferred work behind this
route. The acknowledgment body is:

```json
{"accepted": true, "personal_best": 32768, "high_score_updated": true}
```

The `tui-gamepigeon` score outbox (`HttpResultTransport`) treats any status
other than `200` as a transport failure, so this route must not be changed to
`202 Accepted`. Asynchronous gameplay ingestion (`POST /v1/game-event-chunks`)
is different and intentionally keeps `202 Accepted`.

These resources do not form one global transaction. A Durable Object serializes
transitions for its own chunk and has strongly consistent private storage. D1
provides transactional database operations, while each R2 write and Queue send
has its own success boundary. Deterministic keys, publication leases, cached
acknowledgments, Queue retries, and the DLQ make those boundaries recoverable;
they do not claim cross-service strong consistency. Queue delivery is at least
once, and the native rate limiter is enforced per Cloudflare location.

## Multiplayer 2048/1v1

The implemented multiplayer mode is 2048/1v1 with exactly two players. It gives
them two independent boards that are visible to both players. Players may submit
moves concurrently; neither player owns a turn. No-op moves consume nothing and
return no error: they do not advance board or room versions, consume RNG state,
or write an action. The room completes only when both boards are finished. The
higher score wins; equal scores are a draw.

An accepted legal move changes only the actor's board; only that board's RNG
state and board version advance. The room persists the action and new room
version before broadcasting a snapshot containing both boards.

The five multiplayer routes are:

| Route | Authentication | Implemented behavior |
| --- | --- | --- |
| `POST /v1/rooms` | Bearer token | Create a `2048`/`1v1` room and return its snapshot and invite URL. |
| `GET /v1/rooms/{room_id}` | Bearer token | Return the room snapshot to a participant. |
| `POST /v1/rooms/{room_id}/join` | Bearer token | Claim the second seat and return the updated snapshot. |
| `POST /v1/rooms/{room_id}/tickets` | Bearer token | Issue the participant's one-time WebSocket ticket. |
| `GET /v1/rooms/{room_id}/socket?ticket={ticket}` | One-time ticket | Upgrade directly to the room's WebSocket. |

The authenticated ticket sequence is:

1. Player one sends a bearer-authenticated `POST /v1/rooms` with
   `{"game_type":"2048","mode":"1v1"}` and shares the returned invite URL.
2. Player two sends a bearer-authenticated `POST /v1/rooms/{room_id}/join`.
3. Either participant can refresh state with the bearer-authenticated
   `GET /v1/rooms/{room_id}` route.
4. Each participant requests a one-time WebSocket ticket with the
   bearer-authenticated `POST /v1/rooms/{room_id}/tickets` route.
5. The client opens `GET /v1/rooms/{room_id}/socket?ticket={ticket}` with a
   WebSocket upgrade. `GAME_ROOMS` consumes the ticket once, attaches the
   participant to that room, and sends the visible room snapshot.

## Prerequisites

- Python 3.13
- `uv` to bootstrap the locked development environment
- Node.js 22.x, pinned to `22.23.1` in `.node-version`, for pywrangler tooling
- Wrangler `4.112.0`, pinned by the Make targets that touch Cloudflare
- A Cloudflare account authenticated for Wrangler

```bash
uv sync --frozen
make node-preflight
uv run --frozen uv --version
npx --yes wrangler@4.112.0 login
```

The version check must print `uv 0.10.9`. The project intentionally installs
that exact development-only version because pywrangler 1.15 uses `uv`
internally and later releases reject the Pyodide 0.28.3 `pydantic-core` wheel
selected for Python 3.13. Running pywrangler through `uv run --frozen` puts the
project-local executable first on `PATH`, even when the outer bootstrap `uv` is
newer.

Node.js major 22 is required, and `22.23.1` is the tested repository pin.
Node.js 23 and newer are unsupported by this current pywrangler toolchain:
pywrangler's Pyodide interpreter probe uses a V8 flag that Node 26 rejects.
`make node-preflight` checks the `node` selected by `PATH` before every
pywrangler-facing Make target and reports how to select the pinned release.

`uv.lock` reproduces the CPython development environment. The committed
`pylock.toml` is pywrangler's generated Worker dependency lock and pins the
packages installed into Pyodide. Do not hand-edit either lock. A maintainer can
intentionally refresh both after compatibility testing with:

```bash
uv lock
uv sync --frozen
make node-preflight
uv run --frozen pywrangler sync --force --upgrade
```

Fresh `dev` and `deploy` commands automatically synchronize the committed
Worker lock; a separate sync step is not required for a clean checkout.
Use `make worker-sync` to materialize and verify it explicitly.
Cloudflare-facing Make targets call `wrangler@4.112.0` explicitly so releases
cannot silently move to a newly published CLI version.

`.env.example` contains only the optional, non-secret account selector. Do not
put API tokens, bearer tokens, or service credentials in `wrangler.toml` or a
committed environment file.

## Provisioning

The following commands create remote account resources and should be run only
when intentionally provisioning an environment:

```bash
make node-preflight
npx --yes wrangler@4.112.0 d1 create retro-backend
npx --yes wrangler@4.112.0 r2 bucket create retro-gameplay-raw
npx --yes wrangler@4.112.0 queues create retro-gameplay-events
npx --yes wrangler@4.112.0 queues create retro-gameplay-events-dlq
npx --yes wrangler@4.112.0 queues create retro-gameplay-events-archive-dlq
```

Leave the committed `wrangler.toml` template placeholders untouched; the
deployment renderer verifies the template before creating an ignored production
configuration. `INGEST_RATE_LIMITER` uses namespace `1001`; rate-limit namespace
IDs are account-wide, so select another positive integer if `1001` is already
used in the account.

Apply the D1 migration locally before starting the local Worker:

```bash
make migrate-local
```

The committed configuration intentionally retains an all-zero D1 ID and inert
multiplayer URL values so a developer cannot deploy to an implied account or
client. Export the account D1 UUID and canonical public values, then render an
ignored, account-specific production configuration:

```bash
export CLOUDFLARE_D1_DATABASE_ID="<uuid-from-d1-create>"
export PUBLIC_GAME_URL="<canonical-public-game-url>"
export ALLOWED_GAME_ORIGINS="<comma-separated-canonical-origins>"
make prepare-deployment-config
make deployment-preflight DEPLOY_CONFIG=wrangler.production.toml
```

The angle-bracket URL values are deliberately invalid operator replacement
tokens, not deploy-ready defaults. Replace them in the protected environment
with canonical URLs on domains the operator controls before rendering.

The generated `wrangler.production.toml` is ignored by git. The preflight
checks the D1 identity, rendered multiplayer URLs, exact custom domain,
production mode, observability, Queue and DLQ consumers, and the checked-in R2
lifecycle policy.

Only after rendering and preflight succeeds, review the target account and D1
migration, then run the remote migration against that same generated config:

```bash
make migrate-remote DEPLOY_CONFIG=wrangler.production.toml
```

The remote command mutates the configured Cloudflare account.

Apply the lifecycle rule after creating the bucket:

```bash
make apply-r2-lifecycle DEPLOY_CONFIG=wrangler.production.toml
```

The policy retains every raw, validated, rejected, and DLQ artifact while
transitioning objects to R2 Infrequent Access after 30 days. It does not delete
training data.

## Local Development

```bash
uv sync --frozen
make migrate-local
make dev
```

The local API is then available at `http://127.0.0.1:8787`. Useful routes are:

- `GET /health/live`
- `GET /health/ready`
- `GET /v1/me`
- `POST /v1/auth/logout` (`?scope=session|all_devices`, default `session`)
- `POST /v1/game-event-chunks` (`202 Accepted` on success)
- `POST /v1/game-results` (`200 OK` on success)
- `GET /v1/high-scores/{game_type}`

Requests to authenticated routes use `Authorization: Bearer <opaque-token>`.

## User Tokens

Generate a one-time opaque token and hash-only insert template with:

```bash
uv run --frozen python scripts/issue_token.py > /tmp/retro-user.sql
```

The token is printed once to stderr, while stdout contains two `INSERT`s (a
`users` row and an `auth_sessions` row) with a `?1` placeholder for the user ID
and only the SHA-256 token digest. Bind a trusted
`usr_...` ID to `?1` through a prepared D1 statement, then run it in the target
database. Deliver the token to the user over an appropriate secret channel. Do
not store the plaintext token in D1, source control, logs, or analytics.

The ID bound to `?1` must be the **canonical** user ID,
`usr_ + sha256(auth_user_id)[:32]` (`canonical_user_id` in
`src/retro_backend/auth.py`) — the same value `GET /v1/me` returns. Binding a
raw upstream auth ID here makes every chunk upload fail with `400`, because
ingestion requires the envelope and all of its events to carry the canonical
ID. See [docs/e2e-local.md](docs/e2e-local.md#the-identity-rule).

Each token is its own `auth_sessions` row, so a user can hold several at once
and signing in on a second device does not sign the first one out. `POST
/v1/auth/logout` revokes the presented token (or all of them with
`?scope=all_devices`) and tombstones the digest so the `AUTH_SESSION_URL`
fallback cannot re-create it. D1 revocation is immediate; a data center that
already cached the digest keeps accepting the token for up to
`AUTH_CACHE_TTL_SECONDS`, which the logout response returns as
`edge_cache_ttl_seconds`. See
[docs/superpowers/specs/2026-07-27-token-revocation-design.md](docs/superpowers/specs/2026-07-27-token-revocation-design.md).

## Configuration

Non-secret Worker variables live in `wrangler.toml`:

- `AUTH_CACHE_TTL_SECONDS=60`
- `AUTH_SESSION_URL=https://auth.runretroarcade.com/api/auth/get-session`
- `MAX_CHUNK_BYTES=262144`
- `MAX_RESULT_BYTES=16384`
- `CHUNK_DEDUP_TTL_MS=172800000`
- `CHUNK_PUBLISHING_LEASE_MS=30000`
- `ENVIRONMENT=production`
- `PUBLIC_GAME_URL=https://gamepigeon.example`
- `ALLOWED_GAME_ORIGINS=https://gamepigeon.example`

`PUBLIC_GAME_URL` is the credential-free canonical HTTP(S) client base used to
build invite URLs. It may include a canonical safe base path.
`ALLOWED_GAME_ORIGINS` is a comma-separated list of unique canonical origins;
each member is exactly `scheme://host[:port]` with no path. The committed
`gamepigeon.example` values are inert placeholders and must be replaced in an
account-specific production configuration. Preflight also rejects every
hostname under `.example`, plus `example.com`, `example.net`, `example.org`, and
all of their subdomains.

Production value placeholders below are deliberately invalid and cannot pass
preflight:

```toml
PUBLIC_GAME_URL = "<canonical-public-game-url>"
ALLOWED_GAME_ORIGINS = "<comma-separated-canonical-origins>"
```

Local example for a client served on port 5173:

```toml
PUBLIC_GAME_URL = "http://localhost:5173/arcade"
ALLOWED_GAME_ORIGINS = "http://localhost:5173,http://127.0.0.1:5173"
```

`GAME_ROOMS` creates one SQLite Durable Object per room. `wrangler.toml`
declares `GameRoom` directly as a SQLite Durable Object export and contains no
top-level Durable Object migrations block. No separate Durable Object migration
command is required; a future real deploy reconciles the declarative export.
This is separate from the existing D1 schema migration commands used for users,
token hashes, and high scores.

Bindings grant the Worker access to D1, R2, Queue, Durable Object, and rate-limit
resources without embedding account credentials. Any future runtime secret must
be added through `npx --yes wrangler@4.112.0 secret put NAME`, never as a
literal in the configuration or source.

## Local Verification

```bash
make verify
make worker-sync
make worker-types
make worker-dry-run
```

These commands run repository-local tests, lint, type checking, dependency
materialization, Worker type generation, and Wrangler's non-deploying dry run.

To prove the full ingestion contract against a running local Worker — auth,
`/v1/me`, chunk upload, R2, the queue consumer's derived artifact, and the D1
high score — start `make dev` in one shell and run:

```bash
make e2e-local
```

See [docs/e2e-local.md](docs/e2e-local.md) for the step-by-step runbook, the
canonical-identity rule clients must follow, and notes on opaque token digests
versus `AUTH_SESSION_URL`.

## Production Deployment

The guarded production commands remain available for an explicitly authorized
account operation:

```bash
make deployment-preflight DEPLOY_CONFIG=wrangler.production.toml
make migrate-remote DEPLOY_CONFIG=wrangler.production.toml
make apply-r2-lifecycle DEPLOY_CONFIG=wrangler.production.toml
make deploy DEPLOY_CONFIG=wrangler.production.toml
```

The final three commands mutate the configured Cloudflare account. The dry run
does not.

Production releases normally run through the manually dispatched
`Deploy production` GitHub Actions workflow. Configure a protected GitHub
environment named `production` with required reviewer approval and these
secrets:

- `CLOUDFLARE_ACCOUNT_ID`
- `CLOUDFLARE_API_TOKEN`
- `CLOUDFLARE_D1_DATABASE_ID`

`CLOUDFLARE_D1_DATABASE_ID` is a protected GitHub environment secret. Configure
these protected GitHub environment variables with canonical, non-placeholder
production values:

- `PUBLIC_GAME_URL`
- `ALLOWED_GAME_ORIGINS`

The API token needs permission to deploy Workers and custom domains, apply D1
migrations, manage Queue consumers, and write the R2 lifecycle configuration.
The workflow first verifies the selected ref without production secrets. Its
secret-bearing deployment job is hard-gated to `main`, then renders and
validates the account config, performs a Worker dry run, migrates D1, applies
the lifecycle, deploys, and probes
`https://api.runretroarcade.com/health/ready`. Concurrency is serialized so two
production releases cannot overlap.

After the first deployment, configure Cloudflare notifications for Worker
errors and Queue backlog, and alert on any message in
`retro-gameplay-events-archive-dlq`. Python Workers remain a Cloudflare beta
surface, so the Worker dry run and a live health probe are both release gates.

## Deferred Integrations

PostHog analytics and Stripe billing are explicitly deferred. There are no
PostHog or Stripe routes, modules, secrets, bindings, producers, or consumers in
the active Worker, and there are no active tables or persistence for either
integration. Future slices can define their own storage and delivery boundaries
without changing the gameplay Queue contract.
