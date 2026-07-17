# Cloudflare Python Backend Design

**Status:** Proposed for implementation

**Goal:** Replace the development-only FastAPI/PostgreSQL/Redis/Redpanda/S3 stack with a Cloudflare-native backend whose runtime code is entirely Python.

## Scope

This rewrite keeps only the useful external contracts and fixtures from the current backend. It does not migrate data or preserve the current deployment topology because the project has no production data of value.

The first implementation slice delivers:

- Python Worker HTTP API using FastAPI through Cloudflare's ASGI adapter.
- D1 users, bearer-token hashes, high scores, Stripe customer state, and webhook idempotency.
- A Python Durable Object for per-user chunk publication coordination and cached acknowledgments.
- R2 storage for exact immutable gameplay chunk bytes.
- Cloudflare Queues for asynchronous gameplay processing, PostHog delivery, and Stripe webhook processing.
- Python queue consumers for validation, deterministic derived artifacts, and third-party delivery.
- Local tests that exercise the domain logic without requiring a Cloudflare account.

Later data-lake work may add Cloudflare Pipelines and R2 Data Catalog for managed Parquet or Iceberg output. That work is intentionally separate because Pipelines and Data Catalog are beta products and are not required for a durable ingestion API.

## Language Boundary

All application and Worker runtime logic is Python. There will be no TypeScript or JavaScript source files.

Declarative infrastructure remains in formats required by Cloudflare:

- `wrangler.toml` for Worker bindings and triggers.
- SQL files for D1 migrations.
- JSON Schema files for shared wire contracts.
- TOML in `pyproject.toml` for Python dependencies and tooling.

Python Workers are currently beta, but Cloudflare officially supports FastAPI, D1, R2, Queues, Durable Objects, and Workflows from Python Workers. The repository will pin compatible tooling and keep Cloudflare binding access behind small adapters so runtime API changes remain localized.

## Architecture

One deployable Python Worker owns HTTP, Queue, Durable Object, and scheduled entrypoints. The source code remains split by responsibility so consumers can be separated into independent Workers later without changing domain interfaces.

```text
GamePigeon local outbox
        |
        v
Python Worker + FastAPI
  | auth/hash lookup ----------> D1
  | chunk coordination --------> ChunkGate Durable Object
  | exact request bytes -------> R2 raw object
  | small durable pointer -----> gameplay-chunks Queue
  | result upsert -------------> D1 high_scores
  | Stripe webhook -----------> billing-events Queue
  ` product event -----------> analytics-events Queue
                                     |
                   +-----------------+-----------------+
                   |                                   |
                   v                                   v
          Python queue consumers              Python PostHog sender
                   |
                   v
          validated R2 artifacts
```

Cloudflare replaces each former infrastructure dependency directly:

| Former component | Cloudflare-native component | Responsibility |
| --- | --- | --- |
| FastAPI container and Uvicorn | Python Worker with FastAPI ASGI adapter | HTTP routing and validation |
| PostgreSQL | D1 | Small relational product and billing state |
| Redis | Durable Objects | Strongly consistent per-chunk coordination and deduplication |
| Redpanda/Kafka | Queues | Durable asynchronous delivery with retries and a DLQ |
| S3/MinIO | R2 | Raw and derived gameplay artifacts |
| Cron container | Cron Trigger or Python Workflow | Cleanup and maintenance |
| Self-hosted metrics stack | Workers observability plus structured logs | Runtime health and failure visibility |

## Repository Structure

```text
retro-backend/
  src/retro_backend/
    entry.py                 # Worker fetch, queue, and scheduled entrypoints
    app.py                   # FastAPI application and exception mapping
    api/
      health.py              # Liveness and readiness routes
      chunks.py              # Authenticated gameplay ingestion
      results.py             # Result submission and leaderboards
      stripe.py              # Stripe webhook endpoint
    auth.py                  # Bearer parsing, hashing, and D1 lookup
    contracts.py             # Pydantic request and response contracts
    chunk_gate.py            # ChunkGate Durable Object
    storage/
      d1.py                  # D1 statements and row conversion
      r2.py                  # Deterministic object keys and conditional writes
      queues.py              # Queue message envelopes
    consumers/
      gameplay.py            # Raw chunk validation and derived R2 artifacts
      analytics.py           # PostHog Capture API delivery
      billing.py             # Stripe event projection into D1
    integrations/
      posthog.py             # Sanitized PostHog HTTP client
      stripe.py              # Stripe REST and signature verification helpers
  migrations/
    0001_core.sql
  contracts/
    ... existing schemas and fixtures ...
  tests/
    unit/
    integration/
  scripts/
    issue_token.py           # Local opaque token and hash generator
  wrangler.toml
  pyproject.toml
```

No SQLAlchemy, Alembic, Redis client, Kafka client, boto client, Uvicorn, Dockerfile, or Docker Compose file remains in the finished rewrite.

## HTTP Contract

The first release exposes:

```text
GET  /health/live
GET  /health/ready
GET  /v1/me
POST /v1/game-event-chunks
POST /v1/game-results
GET  /v1/high-scores/{game_type}
POST /v1/webhooks/stripe
```

Gameplay and score routes use `Authorization: Bearer <opaque-token>`. Stripe uses its signed webhook header and the exact raw request body.

The v2 gameplay request remains compatible with `interaction-trace-chunk-v2.json`. The API performs bounded-body parsing, schema-envelope checks, and authenticated identity checks. Complete event semantics remain asynchronous.

The Cloudflare acknowledgment removes Kafka-specific offsets:

```json
{
  "accepted": true,
  "duplicate": false,
  "game_id": "game_01",
  "chunk_id": "chk_01",
  "chunk_sequence": 1,
  "accepted_event_range": {"first": 1, "last": 12},
  "queued": true,
  "server_received_at_ms": 1784241600000
}
```

`202 Accepted` means the exact bytes are durable in R2 and the processing pointer was accepted by Queues. It does not mean downstream validation or analytics delivery has completed.

## Gameplay Ingestion Flow

1. Read at most 256 KiB from the request and reject an empty or oversized body.
2. Resolve the bearer token by SHA-256 hash in D1. Raw tokens never enter logs, storage, queue messages, or analytics.
3. Parse only the v2 envelope and verify that chunk and event `user_id` values match the authenticated user.
4. Compute the raw-body SHA-256 and address a `ChunkGate` Durable Object by authenticated user plus chunk ID.
5. Ask the object to reserve publication:
   - Same ID and same hash already accepted: return the cached acknowledgment with `duplicate: true`.
   - Same ID and different hash: return `409`.
   - Same hash currently publishing: return retryable `503`.
   - New identity: record a publishing lease and continue.
6. Write the exact bytes conditionally to a deterministic R2 key:

   ```text
   raw/user=<user_id>/game=<game_id>/chunk=<chunk_id>.json
   ```

   Store the SHA-256, schema version, and authenticated identifiers as custom metadata. A pre-existing object is success only when its stored hash matches.
7. Publish a small JSON pointer to `gameplay-chunks`. The Queue message contains the R2 key, hash, authenticated user ID, game ID, chunk ID, and receipt time. It never contains a bearer token or the full raw body. This avoids the current 128 KB Queue message limit while preserving the 256 KiB HTTP contract.
8. Cache the acknowledgment in the Durable Object and return `202`.

If R2 succeeds and Queue publication fails, the API returns `503` and a retry reuses the matching R2 object before retrying the Queue send. If the Queue accepts immediately before the Worker loses its response, a client retry may enqueue the same pointer again. Consumers therefore remain idempotent.

## Durable Object State

Each `ChunkGate` instance owns one logical `(user_id, chunk_id)` identity and stores:

```text
payload_sha256
state: publishing | accepted
lease_expires_at_ms
ack_json
expires_at_ms
```

The object serializes publication attempts and uses SQLite-backed storage. An alarm deletes the object's storage after the configured deduplication window, initially 48 hours. Expiration affects the cached acknowledgment only; deterministic R2 keys and consumer idempotency protect older retries.

The Durable Object never stores request bytes, auth tokens, gameplay events, or customer analytics.

## Queue Processing And R2

Queues provide at-least-once delivery. The gameplay consumer:

1. Reads the referenced R2 object and verifies its SHA-256 metadata.
2. Performs complete v2 semantic validation.
3. On success, writes a deterministic validated artifact under a date derived from the
   chunk's immutable `created_at_ms` value:

   ```text
   validated/date=YYYY-MM-DD/user=<user_id>/game=<game_id>/chunk=<chunk_id>.json
   ```

4. On malformed data, writes a deterministic rejection record and acknowledges the message so poison data does not retry forever.
5. On transient R2 or service failures, raises so Queues retries the message.
6. After the configured retry count, Queues moves the message to `gameplay-chunks-dlq`.

Derived writes use deterministic keys and content hashes, so replay after a crash produces the same object rather than duplicate data.

Parquet is not generated inside the Python Worker. PyArrow is not a suitable Workers dependency, and custom compaction would recreate infrastructure Cloudflare already provides. A follow-up can send validated rows to Cloudflare Pipelines for managed Parquet or Iceberg output in R2.

## D1 Data Model

D1 stores only product state:

```sql
CREATE TABLE users (
    id TEXT PRIMARY KEY,
    auth_token_hash TEXT NOT NULL UNIQUE,
    joined_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE high_scores (
    user_id TEXT NOT NULL,
    game_type TEXT NOT NULL,
    score INTEGER NOT NULL CHECK (score >= 0),
    achieved_at TEXT NOT NULL,
    PRIMARY KEY (user_id, game_type),
    FOREIGN KEY (user_id) REFERENCES users(id) ON DELETE CASCADE
);

CREATE TABLE stripe_customers (
    user_id TEXT PRIMARY KEY,
    stripe_customer_id TEXT NOT NULL UNIQUE,
    subscription_id TEXT,
    subscription_status TEXT,
    current_period_end TEXT,
    updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    FOREIGN KEY (user_id) REFERENCES users(id) ON DELETE CASCADE
);

CREATE TABLE processed_stripe_events (
    event_id TEXT PRIMARY KEY,
    event_type TEXT NOT NULL,
    processed_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);
```

Gameplay chunks and events never enter D1. High-score submission uses a conditional maximum upsert. Lower or equal retries preserve the original score and achievement timestamp.

## PostHog

Product analytics are isolated from gameplay storage. HTTP handlers emit sanitized CloudEvents to `analytics-events` only after the primary operation succeeds. A Python consumer maps them to PostHog Capture API calls.

Allowed events include sign-in, game start or completion, upload success or retry, score submission, and billing lifecycle changes. Raw trajectory payloads, tokens, Stripe payloads, email addresses, and privileged gameplay events are forbidden.

Each PostHog event uses a deterministic insertion ID derived from the CloudEvent ID so Queue retries do not create duplicate analytics events. PostHog failure never changes a successful gameplay or billing response; Queues retries independently and ultimately uses an analytics DLQ.

## Stripe

Stripe integration remains ordinary outbound HTTPS from the Python Worker.

- Checkout or portal routes call Stripe's REST API with a Worker secret.
- `/v1/webhooks/stripe` reads the exact raw body and verifies `Stripe-Signature` before parsing JSON.
- A verified event is enqueued to `billing-events`, and the endpoint returns `2xx` promptly.
- The Python billing consumer uses one atomic D1 batch to insert `processed_stripe_events` and apply the idempotent projection to `stripe_customers`.
- Billing state changes can emit sanitized analytics events after the D1 transaction succeeds.

The first core implementation includes the webhook boundary, signature verifier interface, and queue envelope. Checkout creation can follow once product and price IDs exist.

## Failure Semantics

| Failure | HTTP or consumer behavior |
| --- | --- |
| Missing or invalid bearer token | `401`, no R2 or Queue work |
| Malformed or unsupported chunk envelope | `400`, no R2 or Queue work |
| Chunk ID reused with different bytes | `409`, existing object untouched |
| Request body above 256 KiB | `413` before full buffering |
| Durable Object unavailable | `503`, client keeps local outbox item |
| R2 write unavailable | `503`, no Queue acknowledgment |
| Queue send unavailable | `503`; matching raw R2 object is reused on retry |
| Response lost after Queue acceptance | Client retry may enqueue a duplicate; consumer keys remain deterministic |
| Consumer semantic validation fails | Rejection artifact plus acknowledgment |
| Consumer transient failure | Queue retry, then DLQ after configured limit |
| PostHog unavailable | Analytics Queue retry; primary request remains successful |
| Invalid Stripe signature | `400`, event is not queued |
| Replayed Stripe event | D1 idempotency row makes processing a no-op |

The GamePigeon client remains the final retry boundary. It deletes a local chunk only after receiving a matching `202` acknowledgment and retains it for timeouts, disconnects, `408`, `429`, or `5xx` responses.

## Security

- Store only SHA-256 bearer-token hashes in D1.
- Use Worker secrets for `STRIPE_SECRET_KEY`, `STRIPE_WEBHOOK_SECRET`, and `POSTHOG_API_KEY`.
- Use Cloudflare bindings for D1, R2, Queues, and Durable Objects so no service credentials appear in code.
- Validate exact authenticated identity before constructing storage keys.
- Keep bucket access private; downloads require an authenticated Worker route if added later.
- Apply per-user rate limits at the Worker boundary before R2 writes.
- Log request IDs, authenticated user IDs, game IDs, chunk IDs, hashes, and outcome codes, but never request bodies or credentials.

## Testing

Testing is split into pure Python domain tests and Cloudflare binding integration tests.

Pure tests cover:

- v2 envelope and identity validation.
- bearer parsing and hashing.
- deterministic R2 and Durable Object names.
- acknowledgment and Queue envelope contracts.
- high-score maximum-upsert decisions.
- Stripe signature verification against fixed fixtures.
- PostHog event sanitization and deterministic insertion IDs.

Integration tests use local fakes around binding protocols to cover:

- first publish, duplicate publish, conflict, in-flight retry, R2 failure, and Queue failure.
- gameplay consumer success, deterministic replay, rejection, retry, and DLQ behavior.
- D1 auth, score, leaderboard, Stripe projection, and webhook idempotency.
- FastAPI route status codes and JSON Schema compatibility.

The deployable Worker is then smoke-tested with `pywrangler dev` against local D1, R2, Queue, and Durable Object bindings. A Cloudflare-account integration test is optional and never required for the default unit suite.

## Delivery Order

1. Replace the dependency and deployment skeleton with Python Workers and `pywrangler`.
2. Port the v2 contracts and route tests.
3. Add D1 migration, bearer auth, result submission, and leaderboards.
4. Add `ChunkGate`, deterministic R2 storage, Queue pointer publication, and acknowledgment caching.
5. Add gameplay Queue validation and deterministic derived R2 writes.
6. Add PostHog analytics Queue and consumer.
7. Add Stripe webhook verification, billing Queue, and D1 projection.
8. Add observability, DLQ configuration, deployment documentation, and local smoke tests.

Each step must leave a runnable, tested Worker. The initial milestone is complete when the API can accept a fixture chunk, persist its exact bytes to local R2, enqueue a pointer, consume it into a deterministic validated object, and accept an idempotent high score without PostgreSQL, Redis, Kafka, S3 credentials, Docker, or TypeScript.

## Explicit Non-Goals

- Migrating existing development data.
- Preserving Kafka partition or offset fields.
- Running CPython servers, containers, Redis, PostgreSQL, or Redpanda alongside Cloudflare.
- Generating Parquet with PyArrow inside Workers.
- Sending raw gameplay traces or Stripe webhook bodies to PostHog.
- Building a complete customer sign-up UI or choosing Stripe products and prices in the core ingestion milestone.
