import pytest
from pydantic import ValidationError

from retro_backend.config import Settings


def test_kafka_send_timeout_must_be_shorter_than_publication_lease() -> None:
    with pytest.raises(ValidationError, match="kafka_send_timeout_seconds"):
        Settings(chunk_publishing_ttl_seconds=5, kafka_send_timeout_seconds=5)
