# Retro Backend

Retro is an all-Python Cloudflare Worker for authenticated gameplay ingestion,
score APIs, and asynchronous gameplay artifact processing. FastAPI handles HTTP
through Cloudflare's ASGI adapter; D1, R2, Queues, a Durable Object, and the
native rate limiter are exposed through Worker bindings.

This repository contains deployment configuration and commands, but it has not
been deployed to a Cloudflare account by this implementation.

## Architecture And Request Flow

The Worker may execute in any Cloudflare location and an isolate may be
restarted at any time. Request state therefore stays local to a handler. Durable
state is kept in bindings:

- D1 stores users, token hashes, high scores, and small relational state.
- R2 stores exact request bytes and deterministic derived gameplay artifacts.
- `CHUNK_GATE` is one globally addressed Durable Object per logical user/chunk.
- `GAMEPLAY_QUEUE` carries small pointers to R2 objects, never full trajectories.
- `INGEST_RATE_LIMITER` limits authenticated ingestion before storage work.

For `POST /v1/game-event-chunks`, the Worker authenticates against a token hash
in D1, applies the native rate limit, validates a bounded body, and reserves the
logical chunk in `CHUNK_GATE`. It writes the exact bytes to R2 before publishing
a small object pointer to `retro-gameplay-events`. Only after Queue publication
does the Durable Object persist the accepted acknowledgment. The consumer loads
the exact object from R2, validates it again, and writes deterministic derived
objects before acknowledging the Queue message.

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
- Node.js 20 for pywrangler's local Pyodide and workerd tooling
- A Cloudflare account authenticated for pywrangler

```bash
uv sync --frozen
uv run --frozen uv --version
uv run --frozen pywrangler login
```

The version check must print `uv 0.10.9`. The project intentionally installs
that exact development-only version because pywrangler 1.15 uses `uv`
internally and later releases reject the Pyodide 0.28.3 `pydantic-core` wheel
selected for Python 3.13. Running pywrangler through `uv run --frozen` puts the
project-local executable first on `PATH`, even when the outer bootstrap `uv` is
newer.

`uv.lock` reproduces the CPython development environment. The committed
`pylock.toml` is pywrangler's generated Worker dependency lock and pins the
packages installed into Pyodide. Do not hand-edit either lock. A maintainer can
intentionally refresh both after compatibility testing with:

```bash
uv lock
uv sync --frozen
uv run --frozen pywrangler sync --force --upgrade
```

Fresh `dev` and `deploy` commands automatically synchronize the committed
Worker lock; a separate sync step is not required for a clean checkout.

`.env.example` contains only the optional, non-secret account selector. Do not
put API tokens, bearer tokens, or service credentials in `wrangler.toml` or a
committed environment file.

## Provisioning

The following commands create remote account resources and should be run only
when intentionally provisioning an environment:

```bash
uv run --frozen pywrangler d1 create retro-backend
uv run --frozen pywrangler r2 bucket create retro-gameplay-raw
uv run --frozen pywrangler queues create retro-gameplay-events
uv run --frozen pywrangler queues create retro-gameplay-events-dlq
```

Replace the all-zero `database_id` in `wrangler.toml` with the UUID returned by
the D1 create command. `INGEST_RATE_LIMITER` uses namespace `1001`; rate-limit
namespace IDs are account-wide, so select another positive integer if `1001` is
already used in the account.

Apply the D1 migration locally or remotely:

```bash
uv run --frozen pywrangler d1 migrations apply retro-backend --local
uv run --frozen pywrangler d1 migrations apply retro-backend --remote
```

The remote command mutates the configured Cloudflare account. Review the target
account and migration before running it.

## Local Development

```bash
uv sync --frozen
make migrate-local
uv run --frozen pywrangler dev --local --ip 127.0.0.1 --port 8787
```

The local API is then available at `http://127.0.0.1:8787`. Useful routes are:

- `GET /health/live`
- `GET /health/ready`
- `GET /v1/me`
- `POST /v1/game-event-chunks`
- `POST /v1/game-results`
- `GET /v1/leaderboards/{game_type}`

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

- `MAX_CHUNK_BYTES=262144`
- `CHUNK_DEDUP_TTL_MS=172800000`
- `CHUNK_PUBLISHING_LEASE_MS=30000`
- `ENVIRONMENT=development`

Bindings grant the Worker access to D1, R2, Queue, Durable Object, and rate-limit
resources without embedding account credentials. Any future runtime secret must
be added through `uv run --frozen pywrangler secret put NAME`, never as a
literal in the configuration or source.

## Verification And Deployment

```bash
make verify
uv run --frozen pywrangler types
uv run --frozen pywrangler deploy --dry-run
uv run --frozen pywrangler deploy
```

The final command performs a live deployment; the preceding dry run does not.

## Deferred Integrations

PostHog analytics and Stripe billing are explicitly deferred. There are no
PostHog or Stripe routes, modules, secrets, bindings, producers, or consumers in
the active Worker. A later analytics slice can add a dedicated Queue and
consumer at the gameplay consumer boundary. A later billing slice can add a
raw-body webhook, dedicated Queue, consumer, and required secrets without
changing the gameplay Queue contract.
