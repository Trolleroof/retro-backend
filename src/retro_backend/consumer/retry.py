"""Capped exponential retry helpers for transient dependencies."""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import TypeVar

from aiokafka.errors import (
    CommitFailedError,
    IllegalGenerationError,
    RebalanceInProgressError,
    UnknownMemberIdError,
)
from botocore.exceptions import (
    ClientError,
    ConnectionClosedError,
    ConnectTimeoutError,
    EndpointConnectionError,
    ReadTimeoutError,
)

from .s3_writer import BatchWriter, ObjectConflictError, ObjectReceipt
from .types import BrokerRecord, EventBatch, Partition
from .worker import DlqPublisher, OffsetCommitter

T = TypeVar("T")
logger = logging.getLogger(__name__)

ErrorClassifier = Callable[[Exception], bool]

_TERMINAL_MEMBERSHIP_ERRORS = (
    CommitFailedError,
    IllegalGenerationError,
    RebalanceInProgressError,
    UnknownMemberIdError,
)
_TRANSIENT_S3_CODES = {
    "InternalError",
    "RequestTimeout",
    "RequestTimeoutException",
    "ServiceUnavailable",
    "SlowDown",
    "Throttling",
    "ThrottlingException",
}


def is_retryable_s3_error(error: Exception) -> bool:
    if isinstance(error, ObjectConflictError):
        return False
    if isinstance(
        error,
        (
            ConnectionClosedError,
            ConnectTimeoutError,
            EndpointConnectionError,
            ReadTimeoutError,
            OSError,
        ),
    ):
        return True
    if isinstance(error, ClientError):
        response = error.response
        code = str(response.get("Error", {}).get("Code", ""))
        status = int(response.get("ResponseMetadata", {}).get("HTTPStatusCode", 0))
        return code in _TRANSIENT_S3_CODES or status in {408, 429} or status >= 500
    return False


def is_retryable_kafka_error(error: Exception) -> bool:
    if isinstance(error, _TERMINAL_MEMBERSHIP_ERRORS):
        return False
    return bool(getattr(error, "retriable", False)) or isinstance(error, OSError)


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
    is_retryable: ErrorClassifier = lambda _: True,
) -> T:
    attempt = 0
    delay = policy.initial_delay_seconds
    while True:
        attempt += 1
        try:
            return await operation()
        except asyncio.CancelledError:
            raise
        except Exception as error:
            if not is_retryable(error):
                raise
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
            is_retryable=is_retryable_s3_error,
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
            is_retryable=is_retryable_kafka_error,
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
            is_retryable=is_retryable_kafka_error,
        )
