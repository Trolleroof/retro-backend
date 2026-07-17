"""Create gameplay ingestion and processing tables."""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0001_initial_backend"
down_revision: str | None = None
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "users",
        sa.Column("id", sa.String(length=512), primary_key=True),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
    )
    op.create_table(
        "game_sessions",
        sa.Column("id", sa.String(length=512), primary_key=True),
        sa.Column(
            "user_id",
            sa.String(length=512),
            sa.ForeignKey("users.id", ondelete="RESTRICT"),
            nullable=False,
        ),
        sa.Column("game_type", sa.String(length=128), nullable=False),
        sa.Column("game_version", sa.String(length=64), nullable=False),
        sa.Column("status", sa.String(length=32), server_default="active", nullable=False),
        sa.Column("completed", sa.Boolean(), server_default=sa.false(), nullable=False),
        sa.Column("final_score", sa.Integer(), nullable=True),
        sa.Column("first_event_sequence", sa.BigInteger(), nullable=True),
        sa.Column("last_event_sequence", sa.BigInteger(), nullable=True),
        sa.Column("event_count", sa.Integer(), server_default="0", nullable=False),
        sa.Column("chunk_count", sa.Integer(), server_default="0", nullable=False),
        sa.Column(
            "started_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.Column(
            "updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
    )
    op.create_index("ix_game_sessions_user_id", "game_sessions", ["user_id"])
    op.create_index("ix_game_sessions_game_type", "game_sessions", ["game_type"])
    op.create_index("ix_game_sessions_status", "game_sessions", ["status"])
    op.create_table(
        "ingested_chunks",
        sa.Column("id", sa.String(length=512), primary_key=True),
        sa.Column(
            "game_id",
            sa.String(length=512),
            sa.ForeignKey("game_sessions.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("user_id", sa.String(length=512), nullable=False),
        sa.Column("schema_version", sa.Integer(), nullable=False),
        sa.Column("chunk_sequence", sa.Integer(), nullable=False),
        sa.Column("first_event_sequence", sa.BigInteger(), nullable=False),
        sa.Column("last_event_sequence", sa.BigInteger(), nullable=False),
        sa.Column("event_count", sa.Integer(), nullable=False),
        sa.Column("created_at_ms", sa.BigInteger(), nullable=False),
        sa.Column("payload_sha256", sa.String(length=64), nullable=False),
        sa.Column("raw_payload", sa.LargeBinary(), nullable=False),
        sa.Column(
            "processing_status", sa.String(length=32), server_default="pending", nullable=False
        ),
        sa.Column(
            "received_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.UniqueConstraint("game_id", "chunk_sequence", name="uq_chunk_game_sequence"),
    )
    op.create_index("ix_ingested_chunks_game_id", "ingested_chunks", ["game_id"])
    op.create_index("ix_ingested_chunks_user_id", "ingested_chunks", ["user_id"])
    op.create_index(
        "ix_ingested_chunks_processing_status", "ingested_chunks", ["processing_status"]
    )
    op.create_table(
        "gameplay_events",
        sa.Column("id", sa.String(length=512), primary_key=True),
        sa.Column(
            "chunk_id",
            sa.String(length=512),
            sa.ForeignKey("ingested_chunks.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column(
            "game_id",
            sa.String(length=512),
            sa.ForeignKey("game_sessions.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("user_id", sa.String(length=512), nullable=False),
        sa.Column("event_sequence", sa.BigInteger(), nullable=False),
        sa.Column("event_type", sa.String(length=64), nullable=False),
        sa.Column("visibility", sa.String(length=32), nullable=False),
        sa.Column("client_time_ms", sa.BigInteger(), nullable=False),
        sa.Column("monotonic_time_us", sa.BigInteger(), nullable=False),
        sa.Column("simulation_tick", sa.BigInteger(), nullable=True),
        sa.Column("payload", sa.JSON(), nullable=False),
        sa.UniqueConstraint("game_id", "event_sequence", name="uq_event_game_sequence"),
    )
    op.create_index("ix_gameplay_events_chunk_id", "gameplay_events", ["chunk_id"])
    op.create_index("ix_gameplay_events_game_id", "gameplay_events", ["game_id"])
    op.create_index("ix_gameplay_events_user_id", "gameplay_events", ["user_id"])
    op.create_index("ix_gameplay_events_event_type", "gameplay_events", ["event_type"])
    op.create_index("ix_gameplay_events_visibility", "gameplay_events", ["visibility"])
    op.create_index(
        "ix_events_game_visibility_sequence",
        "gameplay_events",
        ["game_id", "visibility", "event_sequence"],
    )
    op.create_table(
        "processing_jobs",
        sa.Column("id", sa.String(length=600), primary_key=True),
        sa.Column(
            "chunk_id",
            sa.String(length=512),
            sa.ForeignKey("ingested_chunks.id", ondelete="CASCADE"),
            unique=True,
            nullable=False,
        ),
        sa.Column("job_type", sa.String(length=64), server_default="project_chunk", nullable=False),
        sa.Column("status", sa.String(length=32), server_default="pending", nullable=False),
        sa.Column("attempts", sa.Integer(), server_default="0", nullable=False),
        sa.Column(
            "available_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.Column("lease_until", sa.DateTime(timezone=True), nullable=True),
        sa.Column("last_error", sa.Text(), nullable=True),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.Column(
            "updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
    )
    op.create_index("ix_processing_jobs_status", "processing_jobs", ["status"])
    op.create_index("ix_processing_jobs_available_at", "processing_jobs", ["available_at"])


def downgrade() -> None:
    op.drop_table("processing_jobs")
    op.drop_table("gameplay_events")
    op.drop_table("ingested_chunks")
    op.drop_table("game_sessions")
    op.drop_table("users")
