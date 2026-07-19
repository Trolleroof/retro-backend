# Cloudflare Python Backend Design

**Status:** Proposed for implementation

> **Scope update (2026-07-17):** Stripe, PostHog, and analytics or billing queues are deferred by user direction. They have no active routes, resources, secrets, tables, or persistence in this implementation; a short future work note is retained below.

**Goal:** Replace the development-only FastAPI/PostgreSQL/Redis/Redpanda/S3 stack with a Cloudflare-native backend whose runtime code is entirely Python.

## Scope

This rewrite keeps only the useful external contracts and fixtures from the current backend. It does not migrate data or preserve the current deployment topology because the project has no production data of value.

The first implementation slice delivers:

- Python Worker HTTP API using FastAPI through Cloudflare's ASGI adapter.
- D1 users, bearer-token hashes, and high scores.
- A Python Durable Object for per-user chunk publication coordination and cached acknowledgments.
- R2 storage for exact immutable gameplay chunk bytes.
- One Cloudflare Queue for asynchronous gameplay processing.
- A Python queue consumer for validation and deterministic derived artifacts.
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
  | positive auth hit ---------> Cloudflare edge cache
  | auth cache miss -----------> D1
  | chunk coordination --------> ChunkGate Durable Object
  | exact request bytes -------> R2 raw object
  | small durable pointer -----> retro-gameplay-events Queue
  | result upsert -------------> D1 high_scores
                                     |
                                     v
                            Python gameplay consumer
                                     |
                                     v
                            validated R2 artifacts
```

Cloudflare replaces each former infrastructure dependency directly:

| Former component | Cloudflare-native component | Responsibility |
| --- | --- | --- |
| FastAPI container and Uvicorn | Python Worker with FastAPI ASGI adapter | HTTP routing and validation |
| PostgreSQL | D1 | Small relational gameplay product state |
| Redis | Cache API plus Durable Objects | Short-lived auth acceleration plus strongly consistent per-chunk coordination and deduplication |
| Redpanda/Kafka | Queues | Durable asynchronous delivery with retries and a DLQ |
| S3/MinIO | R2 | Raw and derived gameplay artifacts |
| Cron container | Cron Trigger or Python Workflow | Cleanup and maintenance |
| Self-hosted metrics stack | Workers observability plus structured logs | Runtime health and failure visibility |

## Repository Structure

```text
retro-backend/
  src/retro_backend/
    entry.py                 # Worker fetch, queue, and scheduled entrypoints
    worker_http.py           # Pre-ASGI Worker body boundary
    app.py                   # FastAPI application and exception mapping
    api/
      health.py              # Liveness and readiness routes
      request_body.py        # Shared bounded request-body reader
      chunks.py              # Authenticated gameplay ingestion
      results.py             # Result submission and leaderboards
    auth.py                  # Bearer parsing, hashing, and D1 lookup
    contracts.py             # Pydantic request and response contracts
    chunk_gate.py            # ChunkGate Durable Object
    storage/
      d1.py                  # D1 statements and row conversion
      r2.py                  # Deterministic object keys and conditional writes
      queues.py              # Queue message envelopes
    consumers/
      gameplay.py            # Raw chunk validation and derived R2 artifacts
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
```

Gameplay and score routes use `Authorization: Bearer <opaque-token>`.

The v2 gameplay request remains compatible with `interaction-trace-chunk-v2.json`. The Worker fetch boundary bounds the raw stream before the ASGI adapter buffers it. FastAPI then performs authentication, bounded-body defense in depth, schema-envelope checks, and authenticated identity checks. Complete event semantics remain asynchronous.

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

`202 Accepted` means the exact bytes are durable in R2 and the processing pointer was accepted by Queues. It does not mean downstream validation has completed.

## Gameplay Ingestion Flow

1. At the Worker fetch boundary, reject a valid `Content-Length` above 256 KiB or read the incoming stream only to that cap. This happens before the ASGI adapter can buffer the request.
2. Rebuild the Worker request from the exact bounded bytes without the original `Content-Length`. The current ASGI adapter makes a second, bounded copy for FastAPI.
3. Run FastAPI authentication by checking a 60-second positive edge-cache entry keyed by the bearer token's SHA-256 hash. Every auth cache miss or cache failure resolves that digest in D1 and fills the cache only after success. Raw tokens never enter cache keys, logs, storage, or queue messages. Leaderboard responses are not cached, and R2 stores only raw and derived gameplay files.
4. Apply the native per-user ingestion rate limit, then run the route-level bounded reader as defense in depth.
5. Parse only the v2 envelope and verify that chunk and event `user_id` values match the authenticated user.
6. Compute the raw-body SHA-256 and address a `ChunkGate` Durable Object by authenticated user plus chunk ID.
7. Ask the object to reserve publication:
   - Same ID and same hash already accepted: return the cached acknowledgment with `duplicate: true`.
   - Same ID and different hash: return `409`.
   - Same hash currently publishing: return retryable `503`.
   - New identity: record a publishing lease and continue.
8. Write the exact bytes conditionally to a deterministic R2 key:

   ```text
   raw/user=<user_id>/game=<game_id>/chunk=<chunk_id>.json
   ```

   Store the SHA-256, schema version, and authenticated identifiers as custom metadata. A pre-existing object is success only when its stored hash matches.
9. Publish a small JSON pointer to `retro-gameplay-events`. The Queue message contains the R2 key, hash, authenticated user ID, game ID, chunk ID, and receipt time. It never contains a bearer token or the full raw body. This avoids the current 128 KB Queue message limit while preserving the 256 KiB HTTP contract.
10. Cache the acknowledgment in the Durable Object and return `202`.

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
6. After the configured retry count, Queues moves the message to `retro-gameplay-events-dlq`.

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

```

Gameplay chunks and events never enter D1. High-score submission uses a conditional maximum upsert. Lower or equal retries preserve the original score and achievement timestamp.

## Deferred Integrations

PostHog analytics and Stripe billing are future work by explicit user direction. The active gameplay Worker defines no routes, queues, bindings, secrets, tables, persistence, or implementation recipe for either integration. A future design must establish those contracts independently before implementation.

## Failure Semantics

| Failure | HTTP or consumer behavior |
| --- | --- |
| Missing or invalid bearer token | `401`, no R2 or Queue work |
| Malformed or unsupported chunk envelope | `400`, no R2 or Queue work |
| Chunk ID reused with different bytes | `409`, existing object untouched |
| Gameplay body above 256 KiB | Direct `413` before ASGI buffering or authentication |
| Result or unknown-route body above 16 KiB | Direct `413` before ASGI buffering or authentication |
| Malformed or invalid result JSON | `422`, no score mutation |
| Durable Object unavailable | `503`, client keeps local outbox item |
| R2 write unavailable | `503`, no Queue acknowledgment |
| Queue send unavailable | `503`; matching raw R2 object is reused on retry |
| Response lost after Queue acceptance | Client retry may enqueue a duplicate; consumer keys remain deterministic |
| Consumer semantic validation fails | Rejection artifact plus acknowledgment |
| Consumer transient failure | Queue retry, then DLQ after configured limit |

The GamePigeon client remains the final retry boundary. It deletes a local chunk only after receiving a matching `202` acknowledgment and retains it for timeouts, disconnects, `408`, `429`, or `5xx` responses.

## Security

- Store only SHA-256 bearer-token hashes in D1.
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

Integration tests use local fakes around binding protocols to cover:

- first publish, duplicate publish, conflict, in-flight retry, R2 failure, and Queue failure.
- gameplay consumer success, deterministic replay, rejection, retry, and DLQ behavior.
- D1 auth, score, and leaderboard behavior.
- FastAPI route status codes and JSON Schema compatibility.

The deployable Worker is then smoke-tested with `pywrangler dev` against local D1, R2, Queue, and Durable Object bindings. A Cloudflare-account integration test is optional and never required for the default unit suite.

## Delivery Order

1. Replace the dependency and deployment skeleton with Python Workers and `pywrangler`.
2. Port the v2 contracts and route tests.
3. Add D1 migration, bearer auth, result submission, and leaderboards.
4. Add `ChunkGate`, deterministic R2 storage, Queue pointer publication, and acknowledgment caching.
5. Add gameplay Queue validation and deterministic derived R2 writes.
6. Add observability, DLQ configuration, deployment documentation, and local smoke tests.

Each step must leave a runnable, tested Worker. The initial milestone is complete when the API can accept a fixture chunk, persist its exact bytes to local R2, enqueue a pointer, consume it into a deterministic validated object, and accept an idempotent high score without PostgreSQL, Redis, Kafka, S3 credentials, Docker, or TypeScript.

## Explicit Non-Goals

- Migrating existing development data.
- Preserving Kafka partition or offset fields.
- Running CPython servers, containers, Redis, PostgreSQL, or Redpanda alongside Cloudflare.
- Generating Parquet with PyArrow inside Workers.
- Implementing the deferred billing or analytics integrations.
