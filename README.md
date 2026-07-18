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
- `uv`
- A Cloudflare account authenticated for pywrangler

```bash
uv sync
uv run pywrangler login
```

`.env.example` contains only the optional, non-secret account selector. Do not
put API tokens, bearer tokens, or service credentials in `wrangler.toml` or a
committed environment file.

## Provisioning

The following commands create remote account resources and should be run only
when intentionally provisioning an environment:

```bash
uv run pywrangler d1 create retro-backend
uv run pywrangler r2 bucket create retro-gameplay-raw
uv run pywrangler queues create retro-gameplay-events
uv run pywrangler queues create retro-gameplay-events-dlq
```

Replace the all-zero `database_id` in `wrangler.toml` with the UUID returned by
the D1 create command. `INGEST_RATE_LIMITER` uses namespace `1001`; rate-limit
namespace IDs are account-wide, so select another positive integer if `1001` is
already used in the account.

Apply the D1 migration locally or remotely:

```bash
uv run pywrangler d1 migrations apply retro-backend --local
uv run pywrangler d1 migrations apply retro-backend --remote
```

The remote command mutates the configured Cloudflare account. Review the target
account and migration before running it.

## Local Development

```bash
uv sync
make migrate-local
uv run pywrangler dev --local --port 8787
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
uv run python scripts/issue_token.py > /tmp/retro-user.sql
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
be added through `uv run pywrangler secret put NAME`, never as a literal in the
configuration or source.

## Verification And Deployment

```bash
make verify
uv run pywrangler types
uv run pywrangler deploy --dry-run
uv run pywrangler deploy
```

The final command performs a live deployment; the preceding dry run does not.

## Deferred Integrations

PostHog analytics and Stripe billing are explicitly deferred. There are no
PostHog or Stripe routes, modules, secrets, bindings, producers, or consumers in
the active Worker. A later analytics slice can add a dedicated Queue and
consumer at the gameplay consumer boundary. A later billing slice can add a
raw-body webhook, dedicated Queue, consumer, and required secrets without
changing the gameplay Queue contract.
