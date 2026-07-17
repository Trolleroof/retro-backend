import asyncio
from types import SimpleNamespace
from typing import Any

from aiokafka.codec import has_zstd

from retro_backend.config import Settings
from retro_backend.producer import KafkaEventProducer


def test_zstd_codec_is_installed() -> None:
    assert has_zstd()


async def test_kafka_producer_uses_durable_idempotent_configuration(monkeypatch: Any) -> None:
    created: dict[str, Any] = {}

    class FakeKafkaClient:
        def __init__(self) -> None:
            self.refresh_calls = 0

        async def force_metadata_update(self) -> bool:
            self.refresh_calls += 1
            return True

    class FakeAIOKafkaProducer:
        def __init__(self, **kwargs: Any) -> None:
            created.update(kwargs)
            created["instance"] = self
            self.started = False
            self.stopped = False
            self.client = FakeKafkaClient()

        async def start(self) -> None:
            self.started = True

        async def stop(self) -> None:
            self.stopped = True

        async def send_and_wait(self, *_args: Any, **_kwargs: Any) -> SimpleNamespace:
            return SimpleNamespace(partition=2, offset=17)

        async def partitions_for(self, _topic: str) -> set[int]:
            return {0}

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

    metadata = await producer.send(topic="test-events", value=b"{}", key=b"game_1", headers=[])
    assert (metadata.partition, metadata.offset) == (2, 17)
    assert await producer.healthcheck() is True
    assert created["instance"].client.refresh_calls == 1
    await producer.stop()
    assert created["instance"].stopped is True


async def test_kafka_healthcheck_rejects_cached_partitions_when_refresh_fails(
    monkeypatch: Any,
) -> None:
    class FailedRefreshClient:
        def __init__(self) -> None:
            self.refresh_calls = 0

        async def force_metadata_update(self) -> bool:
            self.refresh_calls += 1
            return False

    class CachedMetadataProducer:
        def __init__(self) -> None:
            self.client = FailedRefreshClient()
            self.partition_calls = 0

        async def start(self) -> None:
            pass

        async def stop(self) -> None:
            pass

        async def partitions_for(self, _topic: str) -> set[int]:
            self.partition_calls += 1
            return {0}

    instance = CachedMetadataProducer()

    def create_cached_producer(**_kwargs: Any) -> CachedMetadataProducer:
        return instance

    monkeypatch.setattr("retro_backend.producer.AIOKafkaProducer", create_cached_producer)
    producer = KafkaEventProducer(Settings())
    await producer.start()

    assert await producer.healthcheck() is False
    assert instance.client.refresh_calls == 1
    assert instance.partition_calls == 0


async def test_kafka_healthcheck_bounds_metadata_refresh(monkeypatch: Any) -> None:
    class SlowRefreshClient:
        async def force_metadata_update(self) -> bool:
            await asyncio.sleep(0.1)
            return True

    class SlowMetadataProducer:
        def __init__(self) -> None:
            self.client = SlowRefreshClient()

        async def start(self) -> None:
            pass

        async def stop(self) -> None:
            pass

        async def partitions_for(self, _topic: str) -> set[int]:
            return {0}

    def create_slow_producer(**_kwargs: Any) -> SlowMetadataProducer:
        return SlowMetadataProducer()

    monkeypatch.setattr("retro_backend.producer.AIOKafkaProducer", create_slow_producer)
    producer = KafkaEventProducer(Settings(readiness_timeout_seconds=0.01))
    await producer.start()

    assert await producer.healthcheck() is False
