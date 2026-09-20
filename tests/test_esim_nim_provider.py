"""Unit tests for E-Sim NVIDIA NIM provider adapter (#69).

All tests use pure httpx.MockTransport with zero live network calls.
"""

import asyncio
import json
from collections.abc import Mapping, Sequence
from typing import Any, Self, override

import httpx
import pytest

from retro_backend.esim.providers import (
    DEFAULT_MAX_QUEUE_SECONDS,
    DEFAULT_WINDOW_SECONDS,
    ERR_AUTH,
    ERR_BUDGET_TIMEOUT,
    ERR_HTTP,
    ERR_MALFORMED,
    ERR_NETWORK,
    ERR_RATE_LIMIT,
    ERR_TIMEOUT,
    MAX_RETRY_AFTER_SECONDS,
    AdmissionTimeout,
    ModelAdapter,
    ModelReply,
    NimAdapter,
    RollingWindowBudget,
    get_adapter,
    parse_model_spec,
    parse_retry_after,
    register_provider,
    reset_default_budget,
    validate_https_url,
)

# --- HTTPS Enforcement Tests ---


def test_validate_https_url_accepts_valid_https() -> None:
    assert (
        validate_https_url("https://integrate.api.nvidia.com/v1")
        == "https://integrate.api.nvidia.com/v1"
    )
    assert (
        validate_https_url("https://integrate.api.nvidia.com/v1/")
        == "https://integrate.api.nvidia.com/v1"
    )


def test_validate_https_url_rejects_insecure_http() -> None:
    with pytest.raises(ValueError, match="must use HTTPS"):
        validate_https_url("http://integrate.api.nvidia.com/v1")


def test_validate_https_url_rejects_other_schemes() -> None:
    with pytest.raises(ValueError, match="must use HTTPS"):
        validate_https_url("ftp://integrate.api.nvidia.com/v1")


# --- Request Budget Tests ---


class FakeClock:
    """A monotonic clock a test drives, so a 60s window costs no real seconds.

    Sleeping advances the clock rather than the wall, and yields once so other
    tasks get their turn. That is safe against over-admission precisely
    because :meth:`RollingWindowBudget.acquire` holds its lock across the
    wait: only one waiter is ever inside the window check.
    """

    def __init__(self) -> None:
        self.now = 0.0

    def __call__(self) -> float:
        return self.now

    async def sleep(self, seconds: float) -> None:
        self.now += max(0.0, seconds)
        await asyncio.sleep(0)


def budget_with(
    clock: FakeClock,
    max_starts: int = 30,
    *,
    max_queue_seconds: float | None = None,
) -> RollingWindowBudget:
    # The queue deadline defaults to off here so a test can exercise the
    # window itself without tripping the bound that exists for pathological
    # waits; the tests that mean to check the bound pass one explicitly.
    return RollingWindowBudget(
        max_starts=max_starts,
        window_seconds=60.0,
        max_queue_seconds=max_queue_seconds,
        clock=clock,
        sleep=clock.sleep,
    )


def max_starts_in_any_window(starts: Sequence[float], window: float = 60.0) -> int:
    """The largest number of starts falling inside any rolling window."""
    return max(
        (sum(1 for other in starts if start <= other < start + window) for start in starts),
        default=0,
    )


@pytest.mark.asyncio
async def test_budget_admits_a_full_window_without_waiting() -> None:
    clock = FakeClock()
    budget = budget_with(clock, max_starts=30)
    for _ in range(30):
        assert await budget.acquire() == 0.0
    assert clock.now == 0.0


@pytest.mark.asyncio
async def test_budget_makes_the_next_start_wait_for_the_window_to_roll() -> None:
    clock = FakeClock()
    budget = budget_with(clock, max_starts=2)
    await budget.acquire()
    clock.now = 10.0
    await budget.acquire()

    # Two starts are in the window; the third waits for the first to leave it
    # at t=60, not for the second. It began waiting at t=10, so the delay it
    # reports is the 50s it personally spent queued, not the window's length.
    delay = await budget.acquire()
    assert clock.now == pytest.approx(60.0)
    assert delay == pytest.approx(50.0)


@pytest.mark.asyncio
async def test_two_concurrent_sessions_with_retries_never_exceed_the_budget() -> None:
    """The acceptance test: nothing exceeds 30 starts in any rolling 60s.

    Two sessions run concurrently, each retrying every request twice, which is
    the shape that defeated the per-adapter limiter this replaced: 2 callers x
    3 attempts against one account.
    """
    clock = FakeClock()
    budget = budget_with(clock, max_starts=30)
    admitted: list[float] = []

    async def session(requests: int) -> None:
        for _ in range(requests):
            for _attempt in range(3):  # one call plus two retries
                await budget.acquire()
                admitted.append(clock.now)

    await asyncio.gather(session(12), session(12))

    assert len(admitted) == 72
    assert max_starts_in_any_window(admitted) <= 30


@pytest.mark.asyncio
async def test_budget_backs_every_caller_off_after_retry_after() -> None:
    clock = FakeClock()
    budget = budget_with(clock, max_starts=30)
    assert budget.penalize(15.0) == 15.0

    delay = await budget.acquire()
    assert clock.now == pytest.approx(15.0)
    assert delay == pytest.approx(15.0)


def test_budget_caps_an_unreasonable_retry_after() -> None:
    clock = FakeClock()
    budget = budget_with(clock)
    assert budget.penalize(3_600.0) == MAX_RETRY_AFTER_SECONDS
    assert budget.penalize(0) == 0.0
    assert budget.penalize(-5) == 0.0


def test_budget_snapshot_reports_admission_state() -> None:
    clock = FakeClock()
    budget = budget_with(clock, max_starts=2)
    snapshot = budget.snapshot()
    assert snapshot.starts_in_window == 0
    assert snapshot.max_starts == 2
    assert snapshot.waiting == 0
    assert snapshot.admitting


def test_budget_rejects_a_nonsense_configuration() -> None:
    with pytest.raises(ValueError, match="strictly positive"):
        RollingWindowBudget(max_starts=0)
    with pytest.raises(ValueError, match="strictly positive"):
        RollingWindowBudget(max_starts=30, window_seconds=0)
    with pytest.raises(ValueError, match="strictly positive"):
        RollingWindowBudget(max_starts=30, max_queue_seconds=0)


@pytest.mark.asyncio
async def test_a_wait_past_the_queue_deadline_fails_instead_of_stalling() -> None:
    """Admission happens outside the adapter's per-request timeout.

    Without a bound of its own, a throttled call could hold a turn open for
    minutes past the timeout that was supposed to bound it.
    """
    clock = FakeClock()
    budget = budget_with(clock, max_starts=1, max_queue_seconds=10.0)
    await budget.acquire()

    with pytest.raises(AdmissionTimeout, match="could not be admitted within 10s"):
        await budget.acquire()
    # Failed rather than slept: the clock never advanced to the free slot.
    assert clock.now == 0.0


@pytest.mark.asyncio
async def test_the_queue_deadline_admits_a_wait_that_fits_inside_it() -> None:
    clock = FakeClock()
    budget = budget_with(clock, max_starts=1, max_queue_seconds=90.0)
    await budget.acquire()

    delay = await budget.acquire()
    assert delay == pytest.approx(60.0)


def test_the_default_queue_deadline_covers_the_worst_legitimate_wait() -> None:
    """It must never reject a caller that was going to be admitted anyway.

    A full window plus the largest penalty a provider can impose is the
    longest wait that is not pathological.
    """
    assert DEFAULT_MAX_QUEUE_SECONDS == DEFAULT_WINDOW_SECONDS + MAX_RETRY_AFTER_SECONDS


def test_a_budget_timeout_is_not_retryable() -> None:
    """Retrying asks the same exhausted budget the same question.

    Unlike a 429, which says the provider is busy, this says this process has
    no allowance to give. Retrying would multiply one long wait by
    max_attempts.
    """
    from retro_backend.esim.rotation import retryable_model_error

    assert retryable_model_error(f"{ERR_BUDGET_TIMEOUT}: could not be admitted within 180s") is False
    # The provider-side refusal stays retryable; these are different claims.
    assert retryable_model_error(f"{ERR_RATE_LIMIT}: 429 Too Many Requests") is True


@pytest.mark.asyncio
async def test_the_adapter_reports_a_budget_timeout_without_calling_the_provider() -> None:
    calls = 0

    def handle_request(_request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        return httpx.Response(200, json={"choices": [{"message": {"content": "ok"}}]})

    clock = FakeClock()
    budget = budget_with(clock, max_starts=1, max_queue_seconds=10.0)
    adapter = NimAdapter(
        model="vendor/m",
        api_key="test-key",
        budget=budget,
        transport=httpx.MockTransport(handle_request),
    )
    async with adapter:
        await adapter.generate("Hello")
        blocked = await adapter.generate("Hello again")

    assert calls == 1
    assert blocked.error is not None
    assert blocked.error.startswith(ERR_BUDGET_TIMEOUT)
    assert blocked.latency_ms == 0.0


@pytest.mark.parametrize(
    ("header", "expected"),
    [("12", 12.0), ("0.5", 0.5), (" 3 ", 3.0), ("0", None), ("-1", None), (None, None)],
)
def test_parse_retry_after(header: str | None, expected: float | None) -> None:
    assert parse_retry_after(header) == expected


def test_parse_retry_after_ignores_the_http_date_form() -> None:
    assert parse_retry_after("Wed, 21 Oct 2026 07:28:00 GMT") is None


def test_default_budget_is_shared_across_adapters(monkeypatch: pytest.MonkeyPatch) -> None:
    """Two adapters, one allowance -- the bug that motivated #84."""
    monkeypatch.setenv("NVIDIA_API_KEY", "test-key")
    monkeypatch.setenv("ESIM_NIM_MAX_STARTS_PER_WINDOW", "7")
    reset_default_budget()
    try:
        manager = NimAdapter(model="meta/llama-3-70b-instruct")
        worker = NimAdapter(model="meta/llama-3-8b-instruct")
        assert manager.budget is worker.budget
        assert manager.budget.max_starts == 7
    finally:
        reset_default_budget()


# --- NimAdapter Credentials & Initialization Tests ---


def test_nim_adapter_rejects_empty_model() -> None:
    with pytest.raises(ValueError, match="Model identifier must not be empty"):
        NimAdapter(model="", api_key="test-key")


def test_nim_adapter_rejects_missing_api_key(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("NVIDIA_API_KEY", raising=False)
    monkeypatch.delenv("NIM_API_KEY", raising=False)
    with pytest.raises(ValueError, match="NVIDIA NIM API key is required"):
        NimAdapter(model="meta/llama-3-70b-instruct")


def test_nim_adapter_reads_env_api_key(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("NVIDIA_API_KEY", "nv-test-key-123")
    adapter = NimAdapter(model="meta/llama-3-70b-instruct")
    assert adapter.api_key == "nv-test-key-123"


def test_nim_adapter_reads_nim_env_api_key(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("NVIDIA_API_KEY", raising=False)
    monkeypatch.setenv("NIM_API_KEY", "nim-key-456")
    adapter = NimAdapter(model="meta/llama-3-70b-instruct")
    assert adapter.api_key == "nim-key-456"


def test_nim_adapter_enforces_https() -> None:
    with pytest.raises(ValueError, match="must use HTTPS"):
        NimAdapter(
            model="meta/llama-3-70b-instruct",
            api_key="test-key",
            base_url="http://insecure.nvidia.com/v1",
        )


# --- Successful Query Mock Transport Tests ---


@pytest.mark.asyncio
async def test_nim_adapter_generate_success_messages() -> None:
    recorded_requests: list[httpx.Request] = []

    def handle_request(request: httpx.Request) -> httpx.Response:
        recorded_requests.append(request)
        data = json.loads(request.content)
        assert data["model"] == "meta/llama-3-70b-instruct"
        assert data["temperature"] == 0.0
        assert data["max_tokens"] == 150
        assert len(data["messages"]) == 2
        assert request.headers["authorization"] == "Bearer test-key"
        assert request.headers["content-type"] == "application/json"

        body = {
            "id": "chat-123",
            "choices": [
                {
                    "message": {
                        "role": "assistant",
                        "content": '{"action": "seat", "table_id": "table-1"}',
                        "reasoning_content": "table-1 is open",
                    },
                    "finish_reason": "stop",
                }
            ],
            "usage": {
                "prompt_tokens": 42,
                "completion_tokens": 15,
                "total_tokens": 57,
                "completion_tokens_details": {"reasoning_tokens": 9},
            },
        }
        return httpx.Response(200, json=body)

    transport = httpx.MockTransport(handle_request)
    async with NimAdapter(
        model="meta/llama-3-70b-instruct",
        api_key="test-key",
        transport=transport,
    ) as adapter:
        reply = await adapter.generate(
            messages=[
                {"role": "system", "content": "You are a host."},
                {"role": "user", "content": "Seat the guest."},
            ],
            max_tokens=150,
        )

    assert len(recorded_requests) == 1
    assert reply.ok is True
    assert reply.error is None
    assert reply.text == '{"action": "seat", "table_id": "table-1"}'
    assert reply.input_tokens == 42
    assert reply.output_tokens == 15
    assert reply.reasoning == "table-1 is open"
    assert reply.reasoning_tokens == 9
    assert reply.latency_ms > 0.0


@pytest.mark.asyncio
async def test_nim_adapter_generate_with_string_prompt() -> None:
    recorded_requests: list[httpx.Request] = []

    def handle_request(request: httpx.Request) -> httpx.Response:
        recorded_requests.append(request)
        data = json.loads(request.content)
        assert data["messages"] == [{"role": "user", "content": "Raw prompt string"}]
        return httpx.Response(
            200,
            json={
                "choices": [{"message": {"role": "assistant", "content": "Ack"}}],
                "usage": {"prompt_tokens": 5, "completion_tokens": 2},
            },
        )

    transport = httpx.MockTransport(handle_request)
    adapter = NimAdapter(
        model="meta/llama-3-70b-instruct",
        api_key="test-key",
        transport=transport,
    )
    reply = await adapter.generate("Raw prompt string")
    await adapter.close()

    assert reply.ok is True
    assert reply.text == "Ack"
    assert reply.input_tokens == 5
    assert reply.output_tokens == 2


# --- Error Path Mock Transport Tests ---


@pytest.mark.asyncio
async def test_nim_adapter_handles_429_rate_limit() -> None:
    def handle_request(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            429,
            text="Too Many Requests",
            headers={"retry-after": "2"},
        )

    transport = httpx.MockTransport(handle_request)
    adapter = NimAdapter(
        model="meta/llama-3-70b-instruct",
        api_key="test-key",
        transport=transport,
    )
    reply = await adapter.generate("Hello")
    await adapter.close()

    assert reply.ok is False
    assert reply.text is None
    assert reply.input_tokens == 0
    assert reply.output_tokens == 0
    assert reply.latency_ms > 0.0
    assert reply.error is not None
    assert ERR_RATE_LIMIT in reply.error
    assert "retry-after=2" in reply.error


@pytest.mark.asyncio
async def test_a_429_backs_off_the_budget_every_other_caller_shares() -> None:
    """One refusal is evidence about the account, not about one caller.

    Without this, the manager sees the 429 and the four workers behind it each
    spend a request discovering the same closed door.
    """

    def handle_request(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(429, text="Too Many Requests", headers={"retry-after": "30"})

    clock = FakeClock()
    budget = budget_with(clock, max_starts=30)
    adapter = NimAdapter(
        model="meta/llama-3-70b-instruct",
        api_key="test-key",
        budget=budget,
        transport=httpx.MockTransport(handle_request),
    )
    reply = await adapter.generate("Hello")
    await adapter.close()

    assert reply.error is not None
    assert "backing off 30s" in reply.error
    snapshot = budget.snapshot()
    assert snapshot.penalty_seconds == pytest.approx(30.0)
    assert not snapshot.admitting


@pytest.mark.asyncio
async def test_queue_delay_is_reported_apart_from_model_latency() -> None:
    """A throttled run must not read as a slow model."""

    def handle_request(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={
                "model": "vendor/m",
                "choices": [{"finish_reason": "stop", "message": {"content": "ok"}}],
                "usage": {"prompt_tokens": 1, "completion_tokens": 1},
            },
        )

    clock = FakeClock()
    budget = budget_with(clock, max_starts=1)
    adapter = NimAdapter(
        model="vendor/m",
        api_key="test-key",
        budget=budget,
        transport=httpx.MockTransport(handle_request),
    )
    async with adapter:
        first = await adapter.generate("Hello")
        second = await adapter.generate("Hello again")

    assert first.queue_delay_ms == 0.0
    # The second call waited a full window for the first's slot to expire.
    assert second.queue_delay_ms == pytest.approx(60_000.0)
    assert second.latency_ms < 1_000.0


@pytest.mark.asyncio
async def test_nim_adapter_handles_401_auth_error() -> None:
    def handle_request(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(401, text="Unauthorized key")

    transport = httpx.MockTransport(handle_request)
    adapter = NimAdapter(
        model="meta/llama-3-70b-instruct",
        api_key="test-key",
        transport=transport,
    )
    reply = await adapter.generate("Hello")
    await adapter.close()

    assert reply.ok is False
    assert reply.error is not None
    assert ERR_AUTH in reply.error
    assert "401" in reply.error


@pytest.mark.asyncio
async def test_nim_adapter_handles_500_server_error() -> None:
    def handle_request(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(500, text="Internal Gateway Error")

    transport = httpx.MockTransport(handle_request)
    adapter = NimAdapter(
        model="meta/llama-3-70b-instruct",
        api_key="test-key",
        transport=transport,
    )
    reply = await adapter.generate("Hello")
    await adapter.close()

    assert reply.ok is False
    assert reply.error is not None
    assert ERR_HTTP in reply.error
    assert "500" in reply.error


@pytest.mark.asyncio
async def test_nim_adapter_handles_timeout() -> None:
    def handle_request(_request: httpx.Request) -> httpx.Response:
        raise httpx.ReadTimeout("Read timed out after 30s")

    transport = httpx.MockTransport(handle_request)
    adapter = NimAdapter(
        model="meta/llama-3-70b-instruct",
        api_key="test-key",
        transport=transport,
    )
    reply = await adapter.generate("Hello")
    await adapter.close()

    assert reply.ok is False
    assert reply.error is not None
    assert ERR_TIMEOUT in reply.error


@pytest.mark.asyncio
async def test_nim_adapter_handles_network_connection_error() -> None:
    def handle_request(_request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("Failed to establish DNS connection")

    transport = httpx.MockTransport(handle_request)
    adapter = NimAdapter(
        model="meta/llama-3-70b-instruct",
        api_key="test-key",
        transport=transport,
    )
    reply = await adapter.generate("Hello")
    await adapter.close()

    assert reply.ok is False
    assert reply.error is not None
    assert ERR_NETWORK in reply.error


@pytest.mark.asyncio
async def test_nim_adapter_handles_malformed_json() -> None:
    def handle_request(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, text="<html>502 Bad Gateway</html>")

    transport = httpx.MockTransport(handle_request)
    adapter = NimAdapter(
        model="meta/llama-3-70b-instruct",
        api_key="test-key",
        transport=transport,
    )
    reply = await adapter.generate("Hello")
    await adapter.close()

    assert reply.ok is False
    assert reply.error is not None
    assert ERR_MALFORMED in reply.error
    assert "invalid json" in reply.error


@pytest.mark.asyncio
async def test_nim_adapter_handles_missing_choices() -> None:
    def handle_request(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"choices": []})

    transport = httpx.MockTransport(handle_request)
    adapter = NimAdapter(
        model="meta/llama-3-70b-instruct",
        api_key="test-key",
        transport=transport,
    )
    reply = await adapter.generate("Hello")
    await adapter.close()

    assert reply.ok is False
    assert reply.error is not None
    assert ERR_MALFORMED in reply.error
    assert "choices" in reply.error


# --- Registry Tests ---


def test_parse_model_spec_valid() -> None:
    provider, model = parse_model_spec("nim:meta/llama-3-70b-instruct")
    assert provider == "nim"
    assert model == "meta/llama-3-70b-instruct"


def test_parse_model_spec_whitespace_handling() -> None:
    provider, model = parse_model_spec("  NIM : mistralai/mixtral-8x7b-instruct  ")
    assert provider == "nim"
    assert model == "mistralai/mixtral-8x7b-instruct"


def test_parse_model_spec_invalid_no_colon() -> None:
    with pytest.raises(ValueError, match="Expected format: '<provider>:<model>'"):
        parse_model_spec("llama-3-70b-instruct")


def test_parse_model_spec_invalid_empty_parts() -> None:
    with pytest.raises(ValueError, match="Both provider and model must be non-empty"):
        parse_model_spec("nim:")
    with pytest.raises(ValueError, match="Both provider and model must be non-empty"):
        parse_model_spec(":meta/llama-3-70b-instruct")


def test_get_adapter_nim() -> None:
    adapter = get_adapter(
        "nim:meta/llama-3-70b-instruct",
        api_key="test-key",
    )
    assert isinstance(adapter, NimAdapter)
    assert adapter.model == "meta/llama-3-70b-instruct"


def test_get_adapter_unsupported_provider() -> None:
    with pytest.raises(ValueError, match="Unsupported provider 'anthropic'"):
        get_adapter("anthropic:claude-3-5-sonnet")


def test_register_custom_provider() -> None:
    dummy_calls: list[str] = []

    class DummyAdapter(ModelAdapter):
        def __init__(self, model: str) -> None:
            self.model = model

        @override
        async def generate(
            self,
            messages: Sequence[Mapping[str, str]] | str,
            *,
            max_tokens: int | None = None,
        ) -> ModelReply:
            dummy_calls.append(self.model)
            return ModelReply("dummy", 0, 0, 1.0)

        @override
        async def close(self) -> None:
            pass

        @override
        async def __aenter__(self) -> Self:
            return self

        @override
        async def __aexit__(
            self,
            exc_type: type[BaseException] | None,
            exc_val: BaseException | None,
            exc_tb: object | None,
        ) -> None:
            pass

    def dummy_factory(model: str, **_kwargs: Any) -> ModelAdapter:
        return DummyAdapter(model)

    register_provider("custom", dummy_factory)
    adapter = get_adapter("custom:my-special-model")
    assert isinstance(adapter, DummyAdapter)
    assert adapter.model == "my-special-model"


async def test_exhausted_budget_is_reported_as_truncation_not_a_malformed_body() -> None:
    """A reasoning model that runs out of budget is a budget problem.

    Calling it ``malformed_response`` sent whoever read the bundle looking for
    a provider bug, and -- because malformed replies are retryable -- spent two
    more identical calls that truncated identically.
    """
    from retro_backend.esim.providers.base import ERR_TRUNCATED
    from retro_backend.esim.rotation import retryable_model_error

    def handler(request: httpx.Request) -> httpx.Response:
        del request
        return httpx.Response(
            200,
            json={
                "model": "vendor/m",
                "choices": [
                    {
                        "finish_reason": "length",
                        "message": {"content": None, "reasoning_content": "thinking..."},
                    }
                ],
                "usage": {"prompt_tokens": 10, "completion_tokens": 256},
            },
        )

    adapter = NimAdapter(model="vendor/m", api_key="k", transport=httpx.MockTransport(handler))
    async with adapter:
        reply = await adapter.generate([{"role": "user", "content": "hi"}], max_tokens=256)

    assert not reply.ok
    assert reply.error is not None
    assert reply.error.startswith(ERR_TRUNCATED)
    assert "reasoning" in reply.error
    # Retryable: the runner folds this error into the next attempt's prompt as
    # previousError, so a retry asks the model for a shorter answer rather
    # than repeating an unchanged request under the same budget.
    assert retryable_model_error(reply.error) is True


async def test_served_model_is_read_back_from_the_response_body() -> None:
    """The manifest's claim about which model ran must be checkable."""

    def handler(request: httpx.Request) -> httpx.Response:
        del request
        return httpx.Response(
            200,
            json={
                "model": "vendor/actually-served",
                "choices": [{"finish_reason": "stop", "message": {"content": "{}"}}],
                "usage": {"prompt_tokens": 1, "completion_tokens": 1},
            },
        )

    adapter = NimAdapter(model="vendor/asked", api_key="k", transport=httpx.MockTransport(handler))
    async with adapter:
        reply = await adapter.generate([{"role": "user", "content": "hi"}])

    assert reply.ok
    assert reply.served_model == "vendor/actually-served"


async def test_truncation_mid_answer_is_not_reported_as_invalid_json() -> None:
    """A half-written tool call is a budget problem, not a formatting one.

    A model writing its answer directly (rather than reasoning first) leaves a
    partial string when the budget runs out. Parsed as JSON that reads as a
    model unable to follow the format, and -- being retryable -- spends two
    more calls truncating identically.
    """
    from retro_backend.esim.providers.base import ERR_TRUNCATED
    from retro_backend.esim.rotation import retryable_model_error

    def handler(request: httpx.Request) -> httpx.Response:
        del request
        return httpx.Response(
            200,
            json={
                "model": "vendor/m",
                "choices": [
                    {
                        "finish_reason": "length",
                        "message": {"content": '{"tool":"prepare","arguments":{"orderId":"'},
                    }
                ],
                "usage": {"prompt_tokens": 40, "completion_tokens": 256},
            },
        )

    adapter = NimAdapter(model="vendor/m", api_key="k", transport=httpx.MockTransport(handler))
    async with adapter:
        reply = await adapter.generate([{"role": "user", "content": "hi"}], max_tokens=256)

    assert reply.error is not None
    assert reply.error.startswith(ERR_TRUNCATED)
    assert "mid-answer" in reply.error
    # Retryable on the same basis as the reasoning-truncation case: the next
    # attempt's prompt carries this error as previousError, nudging brevity.
    assert retryable_model_error(reply.error) is True


async def test_a_truncated_call_still_reports_the_tokens_it_burned() -> None:
    """Reporting zero would understate real usage, and so real cost."""

    def handler(request: httpx.Request) -> httpx.Response:
        del request
        return httpx.Response(
            200,
            json={
                "model": "vendor/m",
                "choices": [{"finish_reason": "length", "message": {"content": None}}],
                "usage": {"prompt_tokens": 700, "completion_tokens": 1024},
            },
        )

    adapter = NimAdapter(model="vendor/m", api_key="k", transport=httpx.MockTransport(handler))
    async with adapter:
        reply = await adapter.generate([{"role": "user", "content": "hi"}], max_tokens=1024)

    assert not reply.ok
    assert reply.input_tokens == 700
    assert reply.output_tokens == 1024


async def test_a_failed_attempt_still_reports_which_model_answered() -> None:
    """Dropping the id on failures could hide a second model in the run.

    servedModel drives the run summary's single-model claim. If the provider
    served something else on the attempt that truncated, losing the id there
    let the bundle report no mismatch while another model had in fact shaped
    the retry that followed.
    """
    from retro_backend.esim.providers.base import ERR_TRUNCATED

    def handler(request: httpx.Request) -> httpx.Response:
        del request
        return httpx.Response(
            200,
            json={
                "model": "vendor/substituted",
                "choices": [{"finish_reason": "length", "message": {"content": None}}],
                "usage": {"prompt_tokens": 5, "completion_tokens": 256},
            },
        )

    adapter = NimAdapter(model="vendor/asked", api_key="k", transport=httpx.MockTransport(handler))
    async with adapter:
        reply = await adapter.generate([{"role": "user", "content": "hi"}], max_tokens=256)

    assert reply.error is not None
    assert reply.error.startswith(ERR_TRUNCATED)
    assert reply.served_model == "vendor/substituted"
