import asyncio
from typing import Any

import pytest
from fakeredis.aioredis import FakeRedis

from retro_backend.config import Settings
from retro_backend.producer import BrokerMetadata
from retro_backend.services.ingestion import (
    BrokerPublishError,
    ChunkPublishingError,
    publish_chunk,
)


class SupersedingProducer:
    def __init__(self, redis: FakeRedis, key: str, *, fail: bool) -> None:
        self.redis = redis
        self.key = key
        self.fail = fail

    async def start(self) -> None:
        pass

    async def stop(self) -> None:
        pass

    async def healthcheck(self) -> bool:
        return True

    async def send(
        self,
        *,
        topic: str,
        value: bytes,
        key: bytes,
        headers: list[tuple[str, bytes]],
    ) -> BrokerMetadata:
        _ = topic, value, key, headers
        await self.redis.set(self.key, "new-owner", ex=30)
        if self.fail:
            raise RuntimeError("broker failed after lease expired")
        return BrokerMetadata(partition=3, offset=42)


class SlowProducer:
    async def start(self) -> None:
        pass

    async def stop(self) -> None:
        pass

    async def healthcheck(self) -> bool:
        return True

    async def send(
        self,
        *,
        topic: str,
        value: bytes,
        key: bytes,
        headers: list[tuple[str, bytes]],
    ) -> BrokerMetadata:
        _ = topic, value, key, headers
        await asyncio.sleep(0.1)
        return BrokerMetadata(partition=3, offset=42)


def envelope() -> dict[str, Any]:
    return {
        "game_id": "game_1",
        "chunk_id": "chk_1",
        "chunk_sequence": 1,
        "first_event_sequence": 1,
        "last_event_sequence": 1,
    }


async def test_stale_failed_owner_cannot_delete_newer_lease() -> None:
    redis = FakeRedis(decode_responses=True)
    key = "retro:chunk:usr_1:chk_1"
    producer = SupersedingProducer(redis, key, fail=True)

    with pytest.raises(BrokerPublishError):
        await publish_chunk(
            redis=redis,
            producer=producer,
            settings=Settings(),
            user_id="usr_1",
            envelope=envelope(),
            raw_payload=b"{}",
        )

    assert await redis.get(key) == "new-owner"


async def test_stale_successful_owner_cannot_overwrite_newer_lease() -> None:
    redis = FakeRedis(decode_responses=True)
    key = "retro:chunk:usr_1:chk_1"
    producer = SupersedingProducer(redis, key, fail=False)

    with pytest.raises(ChunkPublishingError, match="ownership was lost"):
        await publish_chunk(
            redis=redis,
            producer=producer,
            settings=Settings(),
            user_id="usr_1",
            envelope=envelope(),
            raw_payload=b"{}",
        )

    assert await redis.get(key) == "new-owner"


async def test_kafka_send_timeout_is_bounded_below_lease() -> None:
    redis = FakeRedis(decode_responses=True)
    settings = Settings(
        chunk_publishing_ttl_seconds=1,
        kafka_send_timeout_seconds=0.01,
    )

    with pytest.raises(BrokerPublishError, match="event broker unavailable"):
        await publish_chunk(
            redis=redis,
            producer=SlowProducer(),
            settings=settings,
            user_id="usr_1",
            envelope=envelope(),
            raw_payload=b"{}",
        )

    assert await redis.get("retro:chunk:usr_1:chk_1") is None
