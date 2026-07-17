"""Replace PostgreSQL event storage with users and high scores.

Revision ID: 0002_streaming_ingestion
Revises: 0001_initial_backend
"""

from __future__ import annotations

import hashlib
import secrets
from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0002_streaming_ingestion"
down_revision: str | None = "0001_initial_backend"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.drop_table("processing_jobs")
    op.drop_table("gameplay_events")
    op.drop_table("ingested_chunks")
    op.drop_table("game_sessions")

    op.alter_column("users", "created_at", new_column_name="joined_at")
    op.add_column("users", sa.Column("auth_token_hash", sa.String(length=64), nullable=True))
    users = sa.table(
        "users",
        sa.column("id", sa.String(length=512)),
        sa.column("auth_token_hash", sa.String(length=64)),
    )
    bind = op.get_bind()
    for user_id in bind.execute(sa.select(users.c.id)).scalars():
        placeholder = hashlib.sha256(secrets.token_bytes(32)).hexdigest()
        bind.execute(
            users.update().where(users.c.id == user_id).values(auth_token_hash=placeholder)
        )
    op.alter_column("users", "auth_token_hash", nullable=False)
    op.create_check_constraint(
        "ck_users_auth_token_hash_length", "users", "length(auth_token_hash) = 64"
    )
    op.create_unique_constraint("uq_users_auth_token_hash", "users", ["auth_token_hash"])

    op.create_table(
        "high_scores",
        sa.Column(
            "user_id",
            sa.String(length=512),
            sa.ForeignKey("users.id", ondelete="CASCADE"),
            primary_key=True,
        ),
        sa.Column("game_type", sa.String(length=32), primary_key=True),
        sa.Column("score", sa.BigInteger(), nullable=False),
        sa.Column("achieved_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint("score >= 0", name="ck_high_scores_nonnegative_score"),
    )
    op.create_index(
        "ix_high_scores_game_score_desc",
        "high_scores",
        ["game_type", sa.text("score DESC")],
    )


def downgrade() -> None:
    raise RuntimeError("0002_streaming_ingestion is destructive and cannot be downgraded")
