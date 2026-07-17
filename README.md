# retro-backend

Operational backend for Retro Arcade gameplay data. The arcade client records durable, game-agnostic interaction chunks; this service authenticates and validates those chunks, stores exact request bytes plus normalized events in PostgreSQL, and projects them through a separate worker process. RL training is intentionally outside this repository.

## Architecture

- **FastAPI API**: health, authenticated chunk ingestion, game metadata, and ordered event queries.
- **PostgreSQL**: authoritative users, game sessions, exact immutable chunks, normalized events, and durable processing jobs.
- **Worker**: claims jobs with `FOR UPDATE SKIP LOCKED`, projects session ranges/completion, and can later host enrichment/export work.
- **Redis**: readiness, accepted-chunk notifications, and future coordination. PostgreSQL remains authoritative if Redis is unavailable.
- **Alembic**: owns production schema migrations.

This follows Groudon's Python foundation (`uv`, FastAPI, async SQLAlchemy, PostgreSQL, Alembic, Redis, pytest, Ruff, BasedPyright) and its separate ingestion/consumer process shape. Retro starts with a PostgreSQL job queue rather than Redpanda; the worker interface can move to a stream without changing the client contract.

## Local development

```bash
cp .env.example .env
uv sync
docker compose up -d postgres redis
uv run alembic upgrade head
uv run uvicorn retro_backend.main:app --reload
```

Run the worker in a second terminal:

```bash
uv run python -m retro_backend.worker
```

Or start the complete stack:

```bash
docker compose up --build
```

The published ports default to PostgreSQL `5432`, Redis `6379`, and API `8000`.
Override `RETRO_POSTGRES_PORT`, `RETRO_REDIS_PORT`, or `RETRO_API_PORT` when those
ports are already occupied; container-to-container URLs do not change.

## Client configuration

```bash
export GAMEPIGEON_INGEST_URL=http://127.0.0.1:8000/v1/game-event-chunks
export GAMEPIGEON_INGEST_API_KEY=development-only-change-me
```

Requests use `Authorization: Bearer <key>`. Production must replace the development key.

## API

- `GET /health/live`
- `GET /health/ready`
- `POST /v1/game-event-chunks`
- `GET /v1/games/{game_id}`
- `GET /v1/games/{game_id}/events?visibility=policy`
- `GET /metrics`

Identical `(game_id, chunk_id)` retries are accepted idempotently. Reusing a chunk ID or chunk sequence with different bytes returns `409`. The database transaction stores the exact payload, normalized events, session counters, and processing job before returning `202`.

## Verification

```bash
make verify
```
