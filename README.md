# retro-backend

Retro Arcade's authenticated producer API. It accepts versioned gameplay chunks, performs only lightweight envelope validation, and publishes the original HTTP bytes to Redpanda for consumer-side semantic validation. PostgreSQL stores users and per-game personal bests; Redis provides bearer-auth caching and chunk deduplication.

## Architecture

- **FastAPI**: health, authenticated chunk production, game-result submission, and leaderboards.
- **PostgreSQL**: only `users` and `high_scores`. Raw tokens and gameplay events are never stored.
- **Redis**: positive/negative auth caching and per-user chunk publication state.
- **Redpanda**: durable event stream. The API uses an idempotent aiokafka producer with `acks=all` and zstd compression.
- **Consumer**: validates complete v2 traces, writes contiguous partition batches to Parquet, and manually commits offsets only after durable object storage.
- **S3 / MinIO**: raw immutable Parquet plus separately compacted daily datasets. Raw event data never enters PostgreSQL.
- **Alembic**: migrates the original event-store schema forward to the two-table schema.

Full event semantics, score derivation, and session projections belong to downstream consumers. The producer API does not inspect event payloads or derive scores.

## Local development

```bash
cp .env.example .env
uv sync
docker compose up --build
```

The published ports default to PostgreSQL `5432`, Redis `6379`, Redpanda `19092`, API `8000`, MinIO `9000`/`9001`, and consumer metrics `9100`. Every host port has a `RETRO_*_PORT` override. Host Kafka clients use `127.0.0.1:$RETRO_KAFKA_PORT`; Compose services use `redpanda:9092`.

To run services separately:

```bash
docker compose up -d postgres redis redpanda redpanda-init minio minio-init
uv run alembic upgrade head
uv run uvicorn retro_backend.main:app --reload
uv run python -m retro_backend.consumer.main
```

## Token issuance

Issue a user's token once after migrations have run:

```bash
uv run python -m retro_backend.token_cli --user-id usr_example
```

The command prints the only copy of the opaque token. PostgreSQL stores only its SHA-256 hash. To invalidate the old token and issue a replacement:

```bash
uv run python -m retro_backend.token_cli --user-id usr_example --rotate
```

Clients send the issued value as `Authorization: Bearer <token>`. There is no shared ingestion-key setting.

Users retained by the forward migration receive inaccessible placeholder hashes. Each migrated user must be issued a usable token with `--rotate`; token recovery is impossible because raw tokens are never stored.

## Configuration

- `RETRO_DATABASE_URL`
- `RETRO_REDIS_URL`
- `RETRO_KAFKA_BOOTSTRAP_SERVERS`
- `RETRO_KAFKA_TOPIC` (default `retro-game-events`)
- `RETRO_KAFKA_DLQ_TOPIC` (default `retro-game-events-dlq`)
- `RETRO_KAFKA_CONSUMER_GROUP_ID` (default `retro-parquet-writer-v1`; keep stable)
- `RETRO_MAX_REQUEST_BYTES` (default 256 KiB)
- `RETRO_MAX_GAME_RESULT_BYTES` (default 16 KiB)
- `RETRO_CHUNK_PUBLISHING_TTL_SECONDS` (default 30)
- `RETRO_CHUNK_DEDUP_TTL_SECONDS` (default 48 hours)
- `RETRO_KAFKA_SEND_TIMEOUT_SECONDS` (default 20; must be shorter than the publication lease)
- `RETRO_READINESS_TIMEOUT_SECONDS` (default 2)
- `RETRO_CONSUMER_BATCH_MAX_RECORDS`, `RETRO_CONSUMER_BATCH_MAX_ROWS`, and `RETRO_CONSUMER_BATCH_MAX_BYTES`
- `RETRO_CONSUMER_FLUSH_INTERVAL_SECONDS`, `RETRO_CONSUMER_POLL_TIMEOUT_MS`, and `RETRO_CONSUMER_POLL_MAX_RECORDS`
- `RETRO_CONSUMER_RETRY_INITIAL_SECONDS`, `RETRO_CONSUMER_RETRY_MAX_SECONDS`, and `RETRO_CONSUMER_SHUTDOWN_RETRY_ATTEMPTS`
- `RETRO_S3_ENDPOINT_URL`, `RETRO_S3_REGION`, `RETRO_S3_ACCESS_KEY_ID`, and `RETRO_S3_SECRET_ACCESS_KEY`
- `RETRO_S3_BUCKET`, `RETRO_S3_RAW_PREFIX`, and `RETRO_S3_COMPACTED_PREFIX`

## API

- `GET /health/live`
- `GET /health/ready`
- `POST /v1/game-event-chunks`
- `POST /v1/game-results`
- `GET /v1/high-scores/{game_type}?limit=100`
- `GET /metrics`

`POST /v1/game-event-chunks` supports v1 and v2 envelopes, keys records by `game_id`, attaches the authenticated user and chunk ID as the `retro-user-id` and `retro-chunk-id` Kafka headers, and publishes the exact request bytes. Duplicate chunk IDs for the same user normally return the original cached acknowledgment without republishing. Reusing a chunk ID with different bytes returns `409`.

### Publication guarantee

Chunk publication is **at least once**, not API-level exactly once. The broker can acknowledge a record immediately before the process loses its Redis lease or crashes while caching the broker receipt. In that window the API returns `503` (or the client loses the response), and a retry can publish the same payload again. Redis leases prevent concurrent owners from deleting or replacing each other's state, but Redis is not a durable exactly-once log and PostgreSQL has no event or outbox storage.

Consumers must therefore deduplicate by the authenticated `retro-user-id` plus `retro-chunk-id`, and by authenticated user plus each payload `event_id` when materializing individual events. The API preserves the request bytes and exposes those identities without deriving event semantics, so a future consumer can perform this deduplication before applying side effects.

## Parquet consumer

The consumer accepts only complete interaction-trace v2 chunks. The `retro-user-id` Kafka header is authoritative; embedded chunk and event identities must match it. Invalid JSON, unsupported schemas, missing headers, and semantic mismatches are published to the DLQ with source topic/partition/offset and rejection reason headers. The source offset is committed only after that DLQ publish is acknowledged.

Valid records are buffered independently per topic-partition, split at UTC day boundaries, and written with the fixed event schema under deterministic keys:

```text
raw/date=YYYY-MM-DD/topic=<topic>/partition=<partition>/offsets=<first>-<last>.parquet
```

Each object stores its SHA-256 and source offset range as metadata. A replay HEAD-checks the same key: identical bytes are accepted without another upload, while different bytes are treated as a conflict. The worker uses manual Kafka commits and advances only to `last_offset + 1` after the contiguous object is verified. Bounded exponential backoff handles transient Kafka and S3 failures; shutdown and partition revocation flush owned buffers before the consumer leaves the group.

Parquet contains only `schema_version`, authenticated identity, game/chunk/event fields, canonical `payload_json`, broker partition/offset, and ingestion time. It contains no auth tokens or score columns.

## Daily compaction

Compact the previous UTC day, or select a day explicitly:

```bash
make compact
make compact DATE=2026-07-17
docker compose --profile maintenance run --rm compactor --date 2026-07-17
```

Compaction reads one raw date prefix, keeps the earliest broker position for each `event_id`, and writes an idempotent manifest-keyed object under `compacted/date=YYYY-MM-DD/`. It never deletes or rewrites raw objects. Schedule the command daily in the deployment scheduler; Compose exposes it as a one-shot maintenance job rather than embedding a second scheduler.

Supported score game types are `2048`, `tetris`, `snake`, `minesweeper`, `sokoban`, `connect4`, `lightsout`, and `sudoku`. A result changes the personal best only when its score is higher; lower and equal retries preserve the original achievement timestamp.

## Verification

```bash
make verify
docker compose config --quiet
```
