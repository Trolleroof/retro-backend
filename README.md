# retro-backend

Retro Arcade's authenticated producer API. It accepts versioned gameplay chunks, performs only lightweight envelope validation, and publishes the original HTTP bytes to Redpanda for consumer-side semantic validation. PostgreSQL stores users and per-game personal bests; Redis provides bearer-auth caching and chunk deduplication.

## Architecture

- **FastAPI**: health, authenticated chunk production, game-result submission, and leaderboards.
- **PostgreSQL**: only `users` and `high_scores`. Raw tokens and gameplay events are never stored.
- **Redis**: positive/negative auth caching and per-user chunk publication state.
- **Redpanda**: durable event stream. The API uses an idempotent aiokafka producer with `acks=all` and zstd compression.
- **Alembic**: migrates the original event-store schema forward to the two-table schema.

Full event semantics, score derivation, and session projections belong to downstream consumers. The producer API does not inspect event payloads or derive scores.

## Local development

```bash
cp .env.example .env
uv sync
docker compose up --build
```

The published ports default to PostgreSQL `5432`, Redis `6379`, Redpanda `19092`, and API `8000`. Override `RETRO_POSTGRES_PORT`, `RETRO_REDIS_PORT`, `RETRO_KAFKA_PORT`, or `RETRO_API_PORT` when needed.

To run services separately:

```bash
docker compose up -d postgres redis redpanda redpanda-init
uv run alembic upgrade head
uv run uvicorn retro_backend.main:app --reload
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

## Configuration

- `RETRO_DATABASE_URL`
- `RETRO_REDIS_URL`
- `RETRO_KAFKA_BOOTSTRAP_SERVERS`
- `RETRO_KAFKA_TOPIC` (default `retro-game-events`)
- `RETRO_MAX_REQUEST_BYTES` (default 256 KiB)
- `RETRO_CHUNK_PUBLISHING_TTL_SECONDS` (default 30)
- `RETRO_CHUNK_DEDUP_TTL_SECONDS` (default 48 hours)

## API

- `GET /health/live`
- `GET /health/ready`
- `POST /v1/game-event-chunks`
- `POST /v1/game-results`
- `GET /v1/high-scores/{game_type}?limit=100`
- `GET /metrics`

`POST /v1/game-event-chunks` supports v1 and v2 envelopes, keys records by `game_id`, attaches the authenticated user as the `retro-user-id` Kafka header, and publishes the exact request bytes. Duplicate chunk IDs for the same user return the cached broker partition and offset without republishing.

Supported score game types are `2048`, `tetris`, `snake`, `minesweeper`, `sokoban`, `connect4`, `lightsout`, and `sudoku`. A result changes the personal best only when its score is higher; lower and equal retries preserve the original achievement timestamp.

## Verification

```bash
make verify
```
