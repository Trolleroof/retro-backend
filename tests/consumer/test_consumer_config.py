import pytest
from pydantic import ValidationError

from retro_backend.config import Settings


def test_consumer_settings_have_stable_group_and_bounded_defaults() -> None:
    settings = Settings(environment="test")

    assert settings.kafka_consumer_group_id == "retro-parquet-writer-v1"
    assert settings.kafka_dlq_topic == "retro-game-events-dlq"
    assert settings.compaction_part_max_rows > 0
    assert settings.compaction_read_batch_rows > 0
    assert settings.s3_bucket == "retro-game-events"


def test_consumer_settings_reject_invalid_retry_and_compaction_bounds() -> None:
    with pytest.raises(ValidationError):
        Settings(environment="test", compaction_part_max_rows=0)
    with pytest.raises(ValidationError, match="consumer_retry_max_seconds"):
        Settings(
            environment="test",
            consumer_retry_initial_seconds=5,
            consumer_retry_max_seconds=1,
        )
