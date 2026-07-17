import pytest

from retro_backend.consumer.dlq import KafkaDlqPublisher
from retro_backend.consumer.types import BrokerRecord


class FakeProducer:
    def __init__(self) -> None:
        self.calls: list[dict[str, object]] = []

    async def send_and_wait(self, topic: str, **kwargs: object) -> object:
        self.calls.append({"topic": topic, **kwargs})
        return object()


@pytest.mark.asyncio
async def test_dlq_preserves_record_and_adds_reason_source_headers() -> None:
    producer = FakeProducer()
    publisher = KafkaDlqPublisher(producer, topic="retro-game-events-dlq")
    record = BrokerRecord(
        topic="retro-game-events",
        partition=4,
        offset=19,
        timestamp_ms=123,
        key=b"game",
        value=b"bad-json",
        headers=(("retro-user-id", b"usr_1"),),
    )

    await publisher.publish(record, "unsupported schema")

    call = producer.calls[0]
    assert call["topic"] == "retro-game-events-dlq"
    assert call["value"] == b"bad-json"
    header_value = call["headers"]
    assert isinstance(header_value, list)
    headers = dict(header_value)
    assert headers["retro-dlq-reason"] == b"unsupported schema"
    assert headers["retro-source-topic"] == b"retro-game-events"
    assert headers["retro-source-partition"] == b"4"
    assert headers["retro-source-offset"] == b"19"
