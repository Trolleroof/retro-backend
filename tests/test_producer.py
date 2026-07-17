from types import SimpleNamespace
from typing import Any

from aiokafka.codec import has_zstd

from retro_backend.config import Settings
from retro_backend.producer import KafkaEventProducer


def test_zstd_codec_is_installed() -> None:
    assert has_zstd()


async def test_kafka_producer_uses_durable_idempotent_configuration(monkeypatch: Any) -> None:
    created: dict[str, Any] = {}

    class FakeAIOKafkaProducer:
        def __init__(self, **kwargs: Any) -> None:
            created.update(kwargs)
            created["instance"] = self
            self.started = False
            self.stopped = False

        async def start(self) -> None:
            self.started = True

        async def stop(self) -> None:
            self.stopped = True

        async def send_and_wait(self, *_args: Any, **_kwargs: Any) -> SimpleNamespace:
            return SimpleNamespace(partition=2, offset=17)

    monkeypatch.setattr("retro_backend.producer.AIOKafkaProducer", FakeAIOKafkaProducer)
    producer = KafkaEventProducer(
        Settings(kafka_bootstrap_servers="redpanda:9092", kafka_topic="test-events")
    )

    await producer.start()
    assert created["bootstrap_servers"] == "redpanda:9092"
    assert created["acks"] == "all"
    assert created["enable_idempotence"] is True
    assert created["compression_type"] == "zstd"
    assert created["instance"].started is True

    metadata = await producer.send(
        topic="test-events", value=b"{}", key=b"game_1", headers=[]
    )
    assert (metadata.partition, metadata.offset) == (2, 17)
    await producer.stop()
    assert created["instance"].stopped is True
