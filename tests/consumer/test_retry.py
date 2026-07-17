import asyncio

import pytest

from retro_backend.consumer.retry import RetryPolicy, retry_async


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
