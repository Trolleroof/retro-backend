"""Capped exponential retry helpers for transient dependencies."""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import TypeVar

from .s3_writer import BatchWriter, ObjectReceipt
from .types import BrokerRecord, EventBatch, Partition
from .worker import DlqPublisher, OffsetCommitter

T = TypeVar("T")
logger = logging.getLogger(__name__)


@dataclass(frozen=True, slots=True)
class RetryPolicy:
    initial_delay_seconds: float = 0.25
    max_delay_seconds: float = 10.0
    shutdown_attempts: int = 3


async def retry_async(
    operation: Callable[[], Awaitable[T]],
    *,
    policy: RetryPolicy,
    stop_event: asyncio.Event,
    sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
) -> T:
    attempt = 0
    delay = policy.initial_delay_seconds
    while True:
        attempt += 1
        try:
            return await operation()
        except asyncio.CancelledError:
            raise
        except Exception:
            if stop_event.is_set() and attempt >= policy.shutdown_attempts:
                raise
            logger.warning("transient consumer operation failed", exc_info=True)
            await sleep(delay)
            delay = min(delay * 2, policy.max_delay_seconds)


class RetryingBatchWriter:
    def __init__(
        self,
        delegate: BatchWriter,
        *,
        policy: RetryPolicy,
        stop_event: asyncio.Event,
    ) -> None:
        self._delegate = delegate
        self._policy = policy
        self._stop_event = stop_event

    async def write(self, batch: EventBatch) -> ObjectReceipt:
        return await retry_async(
            lambda: self._delegate.write(batch),
            policy=self._policy,
            stop_event=self._stop_event,
        )


class RetryingCommitter:
    def __init__(
        self,
        delegate: OffsetCommitter,
        *,
        policy: RetryPolicy,
        stop_event: asyncio.Event,
    ) -> None:
        self._delegate = delegate
        self._policy = policy
        self._stop_event = stop_event

    async def commit(self, partition: Partition, next_offset: int) -> None:
        await retry_async(
            lambda: self._delegate.commit(partition, next_offset),
            policy=self._policy,
            stop_event=self._stop_event,
        )


class RetryingDlqPublisher:
    def __init__(
        self,
        delegate: DlqPublisher,
        *,
        policy: RetryPolicy,
        stop_event: asyncio.Event,
    ) -> None:
        self._delegate = delegate
        self._policy = policy
        self._stop_event = stop_event

    async def publish(self, value: BrokerRecord, reason: str) -> None:
        await retry_async(
            lambda: self._delegate.publish(value, reason),
            policy=self._policy,
            stop_event=self._stop_event,
        )
