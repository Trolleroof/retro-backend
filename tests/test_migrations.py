from pathlib import Path

import pytest
from alembic import command
from alembic.config import Config

from retro_backend.migration_safety import (
    LEGACY_GAMEPLAY_TABLES,
    LegacyDataExportRequiredError,
    assert_legacy_tables_empty,
)

ROOT = Path(__file__).resolve().parents[1]


def test_migrations_render_complete_offline_sql(capsys: pytest.CaptureFixture[str]) -> None:
    config = Config(str(ROOT / "alembic.ini"))

    command.upgrade(config, "head", sql=True)

    sql = capsys.readouterr().out
    assert "CREATE TABLE high_scores" in sql
    assert "DROP TABLE processing_jobs" in sql
    assert "Legacy gameplay data must be exported before migration" in sql
    assert sql.index("Legacy gameplay data must be exported before migration") < sql.index(
        "DROP TABLE processing_jobs"
    )
    assert "md5(id || ':retro-migration-a') || md5(id || ':retro-migration-b')" in sql
    assert "NULL-migration" not in sql
    expected_lock_order = (
        "processing_jobs",
        "gameplay_events",
        "ingested_chunks",
        "game_sessions",
    )
    assert expected_lock_order == LEGACY_GAMEPLAY_TABLES
    first_check = sql.index("EXISTS (SELECT 1")
    lock_positions: list[int] = []
    drop_positions: list[int] = []
    for table in LEGACY_GAMEPLAY_TABLES:
        lock = f'LOCK TABLE "{table}" IN ACCESS EXCLUSIVE MODE'
        drop = f"DROP TABLE {table}"
        assert lock in sql
        lock_positions.append(sql.index(lock))
        drop_positions.append(sql.index(drop))
        assert lock_positions[-1] < first_check
        assert lock_positions[-1] < drop_positions[-1]
    assert lock_positions == sorted(lock_positions)
    assert drop_positions == sorted(drop_positions)
    assert max(lock_positions) < first_check < min(drop_positions)


def test_populated_legacy_tables_require_export_before_drop() -> None:
    with pytest.raises(LegacyDataExportRequiredError) as error:
        assert_legacy_tables_empty(
            {
                "game_sessions": 2,
                "ingested_chunks": 0,
                "gameplay_events": 9,
                "processing_jobs": 0,
            }
        )

    message = str(error.value)
    assert "export" in message.lower()
    assert "game_sessions=2" in message
    assert "gameplay_events=9" in message


def test_empty_legacy_tables_are_safe_to_drop() -> None:
    assert_legacy_tables_empty(
        {
            "game_sessions": 0,
            "ingested_chunks": 0,
            "gameplay_events": 0,
            "processing_jobs": 0,
        }
    )
