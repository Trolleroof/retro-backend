"""SQLAlchemy models for durable gameplay ingestion and projections."""

from __future__ import annotations

import datetime as dt
from typing import Any

from sqlalchemy import (
    JSON,
    BigInteger,
    Boolean,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    LargeBinary,
    String,
    Text,
    UniqueConstraint,
    func,
)
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column


class Base(DeclarativeBase):
    pass


class User(Base):
    __tablename__ = "users"

    id: Mapped[str] = mapped_column(String(512), primary_key=True)
    created_at: Mapped[dt.datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )


class GameSession(Base):
    __tablename__ = "game_sessions"

    id: Mapped[str] = mapped_column(String(512), primary_key=True)
    user_id: Mapped[str] = mapped_column(
        ForeignKey("users.id", ondelete="RESTRICT"), nullable=False, index=True
    )
    game_type: Mapped[str] = mapped_column(String(128), nullable=False, index=True)
    game_version: Mapped[str] = mapped_column(String(64), nullable=False)
    status: Mapped[str] = mapped_column(String(32), default="active", nullable=False, index=True)
    completed: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    final_score: Mapped[int | None] = mapped_column(Integer, nullable=True)
    first_event_sequence: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    last_event_sequence: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    event_count: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    chunk_count: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    started_at: Mapped[dt.datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    updated_at: Mapped[dt.datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now(), nullable=False
    )


class IngestedChunk(Base):
    __tablename__ = "ingested_chunks"

    id: Mapped[str] = mapped_column(String(512), primary_key=True)
    game_id: Mapped[str] = mapped_column(
        ForeignKey("game_sessions.id", ondelete="CASCADE"), nullable=False, index=True
    )
    user_id: Mapped[str] = mapped_column(String(512), nullable=False, index=True)
    schema_version: Mapped[int] = mapped_column(Integer, nullable=False)
    chunk_sequence: Mapped[int] = mapped_column(Integer, nullable=False)
    first_event_sequence: Mapped[int] = mapped_column(BigInteger, nullable=False)
    last_event_sequence: Mapped[int] = mapped_column(BigInteger, nullable=False)
    event_count: Mapped[int] = mapped_column(Integer, nullable=False)
    created_at_ms: Mapped[int] = mapped_column(BigInteger, nullable=False)
    payload_sha256: Mapped[str] = mapped_column(String(64), nullable=False)
    raw_payload: Mapped[bytes] = mapped_column(LargeBinary, nullable=False)
    processing_status: Mapped[str] = mapped_column(
        String(32), default="pending", nullable=False, index=True
    )
    received_at: Mapped[dt.datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )

    __table_args__ = (
        UniqueConstraint("game_id", "chunk_sequence", name="uq_chunk_game_sequence"),
    )


class GameplayEvent(Base):
    __tablename__ = "gameplay_events"

    id: Mapped[str] = mapped_column(String(512), primary_key=True)
    chunk_id: Mapped[str] = mapped_column(
        ForeignKey("ingested_chunks.id", ondelete="CASCADE"), nullable=False, index=True
    )
    game_id: Mapped[str] = mapped_column(
        ForeignKey("game_sessions.id", ondelete="CASCADE"), nullable=False, index=True
    )
    user_id: Mapped[str] = mapped_column(String(512), nullable=False, index=True)
    event_sequence: Mapped[int] = mapped_column(BigInteger, nullable=False)
    event_type: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    visibility: Mapped[str] = mapped_column(String(32), nullable=False, index=True)
    client_time_ms: Mapped[int] = mapped_column(BigInteger, nullable=False)
    monotonic_time_us: Mapped[int] = mapped_column(BigInteger, nullable=False)
    simulation_tick: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    payload: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False)

    __table_args__ = (
        UniqueConstraint("game_id", "event_sequence", name="uq_event_game_sequence"),
        Index("ix_events_game_visibility_sequence", "game_id", "visibility", "event_sequence"),
    )


class ProcessingJob(Base):
    __tablename__ = "processing_jobs"

    id: Mapped[str] = mapped_column(String(600), primary_key=True)
    chunk_id: Mapped[str] = mapped_column(
        ForeignKey("ingested_chunks.id", ondelete="CASCADE"), unique=True, nullable=False
    )
    job_type: Mapped[str] = mapped_column(String(64), default="project_chunk", nullable=False)
    status: Mapped[str] = mapped_column(String(32), default="pending", nullable=False, index=True)
    attempts: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    available_at: Mapped[dt.datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False, index=True
    )
    lease_until: Mapped[dt.datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    last_error: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[dt.datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    updated_at: Mapped[dt.datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now(), nullable=False
    )
