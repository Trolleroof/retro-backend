"""Data-preserving guards for retiring the legacy PostgreSQL event store."""

from __future__ import annotations

from collections.abc import Mapping

LEGACY_GAMEPLAY_TABLES = (
    "game_sessions",
    "ingested_chunks",
    "gameplay_events",
    "processing_jobs",
)


class LegacyDataExportRequiredError(RuntimeError):
    """Legacy gameplay rows must be exported before their tables can be removed."""


def legacy_lock_sql() -> tuple[str, ...]:
    return tuple(
        f'LOCK TABLE "{table}" IN ACCESS EXCLUSIVE MODE' for table in LEGACY_GAMEPLAY_TABLES
    )


def assert_legacy_tables_empty(row_counts: Mapping[str, int]) -> None:
    populated = {name: count for name, count in row_counts.items() if count > 0}
    if not populated:
        return
    details = ", ".join(f"{name}={populated[name]}" for name in sorted(populated))
    message = f"Legacy gameplay data must be exported before migration; populated tables: {details}. Export and verify these rows, then clear the legacy tables before rerunning Alembic."
    raise LegacyDataExportRequiredError(message)


def offline_legacy_guard_sql() -> str:
    populated = " OR ".join(
        f'EXISTS (SELECT 1 FROM "{table}" LIMIT 1)' for table in LEGACY_GAMEPLAY_TABLES
    )
    return f"""
DO $$
BEGIN
  IF {populated} THEN
    RAISE EXCEPTION 'Legacy gameplay data must be exported before migration; export and clear game_sessions, ingested_chunks, gameplay_events, and processing_jobs before rerunning Alembic.';
  END IF;
END $$;
"""
