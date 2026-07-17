from pathlib import Path

import pytest
from alembic import command
from alembic.config import Config

ROOT = Path(__file__).resolve().parents[1]


def test_migrations_render_complete_offline_sql(capsys: pytest.CaptureFixture[str]) -> None:
    config = Config(str(ROOT / "alembic.ini"))

    command.upgrade(config, "head", sql=True)

    sql = capsys.readouterr().out
    assert "CREATE TABLE high_scores" in sql
    assert "DROP TABLE processing_jobs" in sql
