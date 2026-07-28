PRAGMA foreign_keys = ON;

-- One row per live device token. Replaces the single `users.auth_token_hash`
-- slot so a second sign-in no longer evicts the first device.
CREATE TABLE auth_sessions (
    token_hash TEXT PRIMARY KEY,
    user_id TEXT NOT NULL,
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    FOREIGN KEY (user_id) REFERENCES users(id) ON DELETE CASCADE
);

CREATE INDEX auth_sessions_user_idx ON auth_sessions (user_id);

-- Tombstones for revoked digests. Without them the upstream-session fallback in
-- `require_user_id` would re-create a session for a token the client just
-- logged out, because that fallback treats a D1 miss as "not yet mirrored".
CREATE TABLE revoked_tokens (
    token_hash TEXT PRIMARY KEY,
    revoked_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);

CREATE INDEX revoked_tokens_revoked_at_idx ON revoked_tokens (revoked_at);

INSERT INTO auth_sessions (token_hash, user_id, created_at)
SELECT auth_token_hash, id, joined_at FROM users;

-- `users.auth_token_hash` is retained as a legacy mirror of the most recent
-- token. SQLite cannot drop an implicitly indexed (UNIQUE) column, and dropping
-- the table to rebuild it would fire the ON DELETE CASCADE on high_scores.
