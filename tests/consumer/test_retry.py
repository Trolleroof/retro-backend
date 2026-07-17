import asyncio

import pytest
from aiokafka.errors import CommitFailedError, KafkaConnectionError
from botocore.exceptions import ClientError

from retro_backend.consumer.retry import (
    RetryPolicy,
    is_retryable_kafka_error,
    is_retryable_s3_error,
    retry_async,
)
from retro_backend.consumer.s3_writer import ObjectConflictError


@pytest.mark.asyncio
async def test_retry_async_uses_capped_exponential_backoff_until_success() -> None:
    attempts = 0
    delays: list[float] = []

    async def operation() -> str:
        nonlocal attempts
        attempts += 1
        if attempts < 4:
            raise OSError("temporary")
        return "ok"

    async def sleep(delay: float) -> None:
        delays.append(delay)

    result = await retry_async(
        operation,
        policy=RetryPolicy(initial_delay_seconds=0.1, max_delay_seconds=0.2),
        stop_event=asyncio.Event(),
        sleep=sleep,
    )

    assert result == "ok"
    assert delays == [0.1, 0.2, 0.2]


@pytest.mark.asyncio
async def test_retry_async_stops_after_bounded_shutdown_attempts() -> None:
    stop = asyncio.Event()
    stop.set()
    attempts = 0

    async def operation() -> None:
        nonlocal attempts
        attempts += 1
        raise OSError("still unavailable")

    async def sleep(_: float) -> None:
        return None

    with pytest.raises(OSError, match="still unavailable"):
        await retry_async(
            operation,
            policy=RetryPolicy(
                initial_delay_seconds=0.1,
                max_delay_seconds=0.2,
                shutdown_attempts=2,
            ),
            stop_event=stop,
            sleep=sleep,
        )

    assert attempts == 2


def client_error(code: str, status: int) -> ClientError:
    return ClientError(
        {"Error": {"Code": code, "Message": code}, "ResponseMetadata": {"HTTPStatusCode": status}},
        "PutObject",
    )


@pytest.mark.parametrize(
    "error",
    [
        ObjectConflictError("different bytes"),
        client_error("AccessDenied", 403),
        client_error("InvalidAccessKeyId", 400),
        client_error("NoSuchBucket", 404),
    ],
)
def test_permanent_s3_failures_are_not_retryable(error: Exception) -> None:
    assert not is_retryable_s3_error(error)


@pytest.mark.parametrize(
    "error",
    [
        OSError("connection reset"),
        client_error("SlowDown", 503),
        client_error("Throttling", 429),
        client_error("InternalError", 500),
    ],
)
def test_transient_s3_failures_are_retryable(error: Exception) -> None:
    assert is_retryable_s3_error(error)


def test_kafka_membership_loss_is_terminal_for_generation() -> None:
    assert not is_retryable_kafka_error(CommitFailedError())
    assert is_retryable_kafka_error(KafkaConnectionError("offline"))


@pytest.mark.asyncio
async def test_retry_async_surfaces_nonretryable_error_without_sleeping() -> None:
    attempts = 0
    sleeps = 0

    async def operation() -> None:
        nonlocal attempts
        attempts += 1
        raise ObjectConflictError("different bytes")

    async def sleep(_: float) -> None:
        nonlocal sleeps
        sleeps += 1

    with pytest.raises(ObjectConflictError):
        await retry_async(
            operation,
            policy=RetryPolicy(),
            stop_event=asyncio.Event(),
            sleep=sleep,
            is_retryable=is_retryable_s3_error,
        )

    assert attempts == 1
    assert sleeps == 0
