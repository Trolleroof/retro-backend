# Backend Review Remediation Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Close every backend review finding while preserving event-free PostgreSQL ingestion, lossless legacy migration behavior, replay-safe raw storage, and bounded compaction.

**Architecture:** The producer accepts strict v2 routing envelopes only and still publishes exact request bytes. The consumer processes one Kafka record into one immutable raw Parquet object under a generation-locked partition state, while compaction uses disk-backed deduplication and immutable generation parts followed by a last-step latest manifest. Retry policy is operation-specific and permanent failures terminate instead of spinning.

**Tech Stack:** FastAPI, Pydantic v2, SQLAlchemy/Alembic, Redis, aiokafka/Redpanda, aioboto3/S3, PyArrow/Parquet, SQLite local compaction state, Prometheus, pytest.

---

### Task 1: Lossless migration and v2-only producer

**Files:** `migrations/versions/0002_streaming_ingestion.py`, `tests/test_migrations.py`, `src/retro_backend/contracts.py`, `tests/test_contracts.py`, `tests/test_ingestion_api.py`, `README.md`

- [x] Add failing migration tests proving populated legacy tables raise an export-required error and empty tables are droppable.
- [x] Extract a testable legacy-row guard used by online Alembic upgrades before any drop; offline SQL must emit a guarded PostgreSQL block rather than unconditional drops.
- [x] Add failing API/contract tests for schema v1 and make the producer return a clear `400` before broker publication.
- [x] Re-run migration, contract, and ingestion tests and confirm PostgreSQL still contains only users/high_scores after a fresh upgrade.

### Task 2: Strict and physical-range-safe v2 validation

**Files:** `src/retro_backend/consumer/validation.py`, `tests/consumer/test_validation.py`, `src/retro_backend/consumer/parquet.py`, `tests/consumer/test_parquet.py`

- [x] Add failing tests for booleans in numeric fields, negative values, `2**70`, non-finite floats, non-JSON payload values, malformed envelope links, and malformed known payloads including `episode_ended`.
- [x] Use strict constrained Pydantic types capped to signed int64/int32/int16 physical ranges and discriminated payload models for every known event type.
- [x] Canonically JSON-encode validated payloads with `allow_nan=False`; convert all validation/serialization failures to `RecordValidationError` so the worker sends them to DLQ.
- [x] Verify every produced `EventRow` can be encoded by the fixed Parquet schema.

### Task 3: Generation-safe one-record raw writes

**Files:** `src/retro_backend/consumer/worker.py`, `src/retro_backend/consumer/service.py`, `src/retro_backend/consumer/kafka.py`, `src/retro_backend/consumer/s3_writer.py`, `src/retro_backend/consumer/types.py`, focused consumer tests

- [x] Add failing tests proving every Kafka offset maps to `offset-offset.parquet`, replay is byte/key identical, concurrent flush/revoke cannot double-write, and reassignment accepts its first offset without stale continuity state.
- [x] Replace multi-record buffering with one record batch and protect processing/revocation with a shared async lock.
- [x] On revoke, atomically detach owned state, persist/commit it once, clear generation offsets, and reject stale-generation commits.
- [x] Translate aiokafka membership-loss commit failures into a terminal generation error that bypasses retry.

### Task 4: Retry classification

**Files:** `src/retro_backend/consumer/retry.py`, `src/retro_backend/consumer/s3_storage.py`, `tests/consumer/test_retry.py`, `tests/consumer/test_s3_storage.py`

- [x] Add failing tests for retryable network/timeout/throttle/5xx failures and terminal object conflicts, auth failures, invalid config, ordinary 4xx, and membership loss.
- [x] Add explicit exception classifiers and make `retry_async` retry only when its classifier returns true.
- [x] Preserve capped exponential delay and bounded shutdown attempts; terminal failures surface immediately.

### Task 5: Bounded manifest compaction

**Files:** `src/retro_backend/compaction.py`, `src/retro_backend/consumer/s3_storage.py`, `tests/consumer/test_compaction.py`, `README.md`

- [x] Add failing tests for `(user_id,event_id)` deduplication, same event ID across users, forced multi-part output, late raw data creating a new generation, and latest manifest publication only after all parts succeed.
- [x] Stream raw objects one at a time into a temporary SQLite table with a composite primary key and earliest broker-position upsert.
- [x] Page sorted rows from SQLite into bounded Parquet generation parts; upload immutable parts and then atomically replace `latest.json` with a manifest listing the authoritative generation.
- [x] Keep every raw object and prior compacted generation; failed generations must never become latest.

### Task 6: Rotation, Kafka security, and worker health

**Files:** `src/retro_backend/token_cli.py`, `tests/test_token_issuance.py`, `src/retro_backend/config.py`, `src/retro_backend/producer.py`, `src/retro_backend/consumer/main.py`, `src/retro_backend/consumer/metrics.py`, `docker-compose.yml`, `.env.example`, focused tests

- [x] Add a long-transaction race test proving the old-token tombstone remains longer than positive-cache TTL and is reasserted after commit; preserve rollback cleanup.
- [x] Add validated optional Kafka `security_protocol`, SASL mechanism/username/password, and SSL settings and pass one redacted option mapping to producer, consumer, and DLQ producer.
- [x] Bind the local Redpanda host mapping to `127.0.0.1`.
- [x] Preserve and make green `tests/consumer/test_metrics.py`; add running, heartbeat, progress timestamp/offset metrics and a semantic health probe that fails when stopped or stale.
- [x] Make Compose health call the semantic worker health probe.

### Task 7: Final verification and delivery

**Files:** `README.md`, `.env.example`, all changed implementation/tests

- [x] Run focused tests after each red-green cycle, then `make verify`.
- [x] Run `docker compose config --quiet` and maintenance-profile rendering.
- [x] Exercise the built images on the existing isolated Retro network; verify valid event to one raw object, malformed event to DLQ, committed offsets, latest compaction manifest, and stale health behavior.
- [x] Review the complete diff for credential leakage, event-table writes, unbounded collections, and destructive migration paths.
- [x] Commit and push `codex/streaming-ingestion`.
