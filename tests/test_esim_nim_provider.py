"""Unit tests for E-Sim NVIDIA NIM provider adapter (#69).

All tests use pure httpx.MockTransport with zero live network calls.
"""

import json
import time
from collections.abc import Mapping, Sequence
from typing import Any, Self, override

import httpx
import pytest

from retro_backend.esim.providers import (
    ERR_AUTH,
    ERR_HTTP,
    ERR_MALFORMED,
    ERR_NETWORK,
    ERR_RATE_LIMIT,
    ERR_TIMEOUT,
    AsyncRateLimiter,
    ModelAdapter,
    ModelReply,
    NimAdapter,
    get_adapter,
    parse_model_spec,
    register_provider,
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


# --- Rate Limiter Tests ---


@pytest.mark.asyncio
async def test_async_rate_limiter_initial_burst() -> None:
    # 40 requests/sec with burst capacity of 40 requests
    limiter = AsyncRateLimiter(requests_per_second=40.0, burst_capacity=40.0)
    t0 = time.monotonic()
    for _ in range(10):
        await limiter.acquire()
    elapsed = time.monotonic() - t0
    assert elapsed < 0.1


@pytest.mark.asyncio
async def test_async_rate_limiter_throttles_when_exhausted() -> None:
    # 10 requests/sec with burst capacity of 1 request
    limiter = AsyncRateLimiter(requests_per_second=10.0, burst_capacity=1.0)
    await limiter.acquire()

    t0 = time.monotonic()
    await limiter.acquire()
    elapsed = time.monotonic() - t0
    assert elapsed >= 0.08


def test_async_rate_limiter_invalid_rate() -> None:
    with pytest.raises(ValueError, match="strictly positive"):
        AsyncRateLimiter(requests_per_second=0)
    with pytest.raises(ValueError, match="strictly positive"):
        AsyncRateLimiter(requests_per_second=-5)


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
                    },
                    "finish_reason": "stop",
                }
            ],
            "usage": {
                "prompt_tokens": 42,
                "completion_tokens": 15,
                "total_tokens": 57,
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

