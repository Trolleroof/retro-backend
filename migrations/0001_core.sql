PRAGMA foreign_keys = ON;

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

CREATE INDEX high_scores_leaderboard_idx
ON high_scores (game_type, score DESC, achieved_at ASC, user_id ASC);
