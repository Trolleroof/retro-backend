PRAGMA foreign_keys = ON;

-- Queryable projection of the episode Parquet dataset (retro-backend#31). The
-- EpisodeAssembler Durable Object remains the source of truth for assembly; this
-- table only points operators at the artifacts the Parquet writer produced.
--
-- No FOREIGN KEY to users(id): the Parquet writer must be able to index an
-- episode even if the user row is later removed, and the R2 objects outlive it.
CREATE TABLE IF NOT EXISTS episodes (
  episode_id            TEXT PRIMARY KEY,
  user_id               TEXT NOT NULL,
  game_id               TEXT NOT NULL,
  game_type             TEXT NOT NULL,
  game_version          TEXT NOT NULL,
  status                TEXT NOT NULL,
  chunk_count           INTEGER NOT NULL,
  row_count             INTEGER NOT NULL,
  first_event_sequence  INTEGER NOT NULL,
  last_event_sequence   INTEGER NOT NULL,
  completed_at_ms       INTEGER NOT NULL,
  parquet_key           TEXT NOT NULL,
  parquet_bytes         INTEGER NOT NULL,
  parquet_sha256        TEXT NOT NULL,
  manifest_key          TEXT NOT NULL,
  written_at_ms         INTEGER NOT NULL
);
-- Deliberately NOT unique. game_id is client-chosen (GAME_ID_PATTERN accepts any
-- game_<suffix>), so it is unique only within a user; episode_id is
-- sha256(user_id:game_id) and already enforces one row per (user, game). A
-- UNIQUE index here would make a second user picking the same game_id violate a
-- constraint the upsert's ON CONFLICT(episode_id) clause cannot absorb, failing
-- the write and retrying the message into a dead-letter queue that has no
-- consumer.
CREATE INDEX IF NOT EXISTS episodes_game_id_idx ON episodes (game_id);
CREATE INDEX IF NOT EXISTS episodes_type_completed_idx ON episodes (game_type, completed_at_ms);
CREATE INDEX IF NOT EXISTS episodes_user_completed_idx ON episodes (user_id, completed_at_ms);
