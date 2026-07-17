"""SQLAlchemy models for Retro users and personal best scores."""

from __future__ import annotations

import datetime as dt

from sqlalchemy import BigInteger, CheckConstraint, DateTime, ForeignKey, Index, String, func
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column


class Base(DeclarativeBase):
    pass


class User(Base):
    __tablename__ = "users"

    id: Mapped[str] = mapped_column(String(512), primary_key=True)
    auth_token_hash: Mapped[str] = mapped_column(String(64), unique=True, nullable=False)
    joined_at: Mapped[dt.datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )

    __table_args__ = (
        CheckConstraint("length(auth_token_hash) = 64", name="ck_users_auth_token_hash_length"),
    )


class HighScore(Base):
    __tablename__ = "high_scores"

    user_id: Mapped[str] = mapped_column(
        String(512), ForeignKey("users.id", ondelete="CASCADE"), primary_key=True
    )
    game_type: Mapped[str] = mapped_column(String(32), primary_key=True)
    score: Mapped[int] = mapped_column(BigInteger, nullable=False)
    achieved_at: Mapped[dt.datetime] = mapped_column(DateTime(timezone=True), nullable=False)

    __table_args__ = (
        CheckConstraint("score >= 0", name="ck_high_scores_nonnegative_score"),
        Index("ix_high_scores_game_score_desc", "game_type", score.desc()),
    )
