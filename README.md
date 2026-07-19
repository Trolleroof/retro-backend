# Retro Backend

Retro is an all-Python Cloudflare Worker for authenticated gameplay ingestion,
score APIs, and asynchronous gameplay artifact processing. FastAPI handles HTTP
through Cloudflare's ASGI adapter; D1, R2, Queues, a Durable Object, and the
native rate limiter are exposed through Worker bindings.

The production Worker is configured for the custom domain
`api.runretroarcade.com`. This repository contains guarded deployment
automation, but no live Cloudflare account deployment is performed by the
repository itself.

The root `runretroarcade.com` domain remains available for a future website.
Account authentication can be hosted separately at `auth.runretroarcade.com`;
this Worker currently keeps its hash-only opaque-token authentication contract.

## Architecture And Request Flow

The Worker may execute in any Cloudflare location and an isolate may be
restarted at any time. Request state therefore stays local to a handler. Durable
state is kept in bindings:

- D1 is authoritative for users, token hashes, and high scores.
- Cloudflare's Cache API stores positive token-hash-to-user resolutions briefly
  at the edge; every auth cache miss falls through to D1.
- R2 stores exact request bytes and deterministic derived gameplay artifacts.
- `CHUNK_GATE` is one globally addressed Durable Object per logical user/chunk.
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

Authentication hashes each opaque bearer token before checking
`caches.default`. A valid hit avoids a D1 auth query. A miss, malformed cache
entry, or Cache API failure queries D1 and caches only a successful resolution
for 60 seconds. Invalid tokens are never cached, and raw tokens never enter
cache keys or values. Leaderboard responses are not cached. R2 remains limited
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

These resources do not form one global transaction. A Durable Object serializes
transitions for its own chunk and has strongly consistent private storage. D1
provides transactional database operations, while each R2 write and Queue send
has its own success boundary. Deterministic keys, publication leases, cached
acknowledgments, Queue retries, and the DLQ make those boundaries recoverable;
they do not claim cross-service strong consistency. Queue delivery is at least
once, and the native rate limiter is enforced per Cloudflare location.

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

Replace the all-zero `database_id` in `wrangler.toml` with the UUID returned by
the D1 create command. `INGEST_RATE_LIMITER` uses namespace `1001`; rate-limit
namespace IDs are account-wide, so select another positive integer if `1001` is
already used in the account.

Apply the D1 migration locally or remotely:

```bash
make migrate-local
make migrate-remote
```

The remote command mutates the configured Cloudflare account. Review the target
account and migration before running it.

The committed configuration intentionally retains an all-zero D1 ID so a
developer cannot deploy to an implied account. Render an ignored,
account-specific production configuration from the real D1 UUID:

```bash
export CLOUDFLARE_D1_DATABASE_ID="<uuid-from-d1-create>"
make prepare-deployment-config
make deployment-preflight DEPLOY_CONFIG=wrangler.production.toml
```

The generated `wrangler.production.toml` is ignored by git. The preflight
checks the D1 ID, exact custom domain, production mode, observability, Queue and
DLQ consumers, and the checked-in R2 lifecycle policy.

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
- `POST /v1/game-event-chunks`
- `POST /v1/game-results`
- `GET /v1/high-scores/{game_type}`

Requests to authenticated routes use `Authorization: Bearer <opaque-token>`.

## User Tokens

Generate a one-time opaque token and hash-only insert template with:

```bash
uv run --frozen python scripts/issue_token.py > /tmp/retro-user.sql
```

The token is printed once to stderr, while stdout contains SQL with a `?1`
placeholder for the user ID and only the SHA-256 token digest. Bind a trusted
`usr_...` ID to `?1` through a prepared D1 statement, then run it in the target
database. Deliver the token to the user over an appropriate secret channel. Do
not store the plaintext token in D1, source control, logs, or analytics.

## Configuration

Non-secret Worker variables live in `wrangler.toml`:

- `AUTH_CACHE_TTL_SECONDS=60`
- `MAX_CHUNK_BYTES=262144`
- `MAX_RESULT_BYTES=16384`
- `CHUNK_DEDUP_TTL_MS=172800000`
- `CHUNK_PUBLISHING_LEASE_MS=30000`
- `ENVIRONMENT=production`

Bindings grant the Worker access to D1, R2, Queue, Durable Object, and rate-limit
resources without embedding account credentials. Any future runtime secret must
be added through `npx --yes wrangler@4.112.0 secret put NAME`, never as a
literal in the configuration or source.

## Verification And Deployment

```bash
make verify
make worker-types
make worker-dry-run
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
