"""NVIDIA NIM provider adapter for E-Sim (#69).

Provides a pure httpx.AsyncClient implementation for interacting with NVIDIA NIM
hosted or self-hosted OpenAI-compatible endpoints (https://integrate.api.nvidia.com/v1).

Key design features:
- Explicit 40 requests/sec (40 req/sec) rate limiter to avoid 429 Too Many Requests.
  Note: This limits HTTP request throughput (req/sec), completely distinct from LLM
  input/output generation tokens.
- Strict HTTPS enforcement.
- Hardcoded temperature 0.0 for deterministic evaluation.
- Detailed token consumption and latency measurement on success and error paths.
- Resilient error handling translating network drops, timeouts, 4xx/5xx, and malformed
  responses into structured ModelReply envelopes without raising unhandled exceptions.
"""

import asyncio
import os
import time
from collections.abc import Mapping, Sequence
from typing import override

import httpx

from retro_backend.esim.providers.base import (
    ERR_AUTH,
    ERR_HTTP,
    ERR_MALFORMED,
    ERR_NETWORK,
    ERR_RATE_LIMIT,
    ERR_TIMEOUT,
    ModelAdapter,
    ModelReply,
    validate_https_url,
)

__all__ = [
    "DEFAULT_NIM_BASE_URL",
    "DEFAULT_NIM_RATE_LIMIT_RPS",
    "DEFAULT_NIM_TIMEOUT_SECONDS",
    "AsyncRateLimiter",
    "NimAdapter",
]

DEFAULT_NIM_BASE_URL = "https://integrate.api.nvidia.com/v1"
DEFAULT_NIM_TIMEOUT_SECONDS = 30.0
DEFAULT_NIM_RATE_LIMIT_RPS = 40.0  # 40 requests/second


class AsyncRateLimiter:
    """Thread-safe and async-safe HTTP request rate limiter (req/sec).

    Enforces an upper bound on HTTP requests per second (req/sec).
    Replenishes request permits smoothly based on elapsed monotonic time up to a maximum
    burst capacity. Callers await :meth:`acquire` before issuing each outbound HTTP request.
    """

    def __init__(
        self,
        requests_per_second: float | None = None,
        burst_capacity: float | None = None,
        *,
        rate: float | None = None,
        capacity: float | None = None,
    ) -> None:
        rps = (
            requests_per_second
            if requests_per_second is not None
            else (rate if rate is not None else 40.0)
        )
        burst = (
            burst_capacity
            if burst_capacity is not None
            else (capacity if capacity is not None else rps)
        )
        if rps <= 0:
            raise ValueError(f"requests_per_second must be strictly positive, got: {rps}")
        self.requests_per_second = float(rps)
        self.burst_capacity = float(burst)
        self._available_requests = self.burst_capacity
        self._last_updated = time.monotonic()
        self._lock = asyncio.Lock()

    async def acquire(self) -> None:
        """Acquire permit for 1 HTTP request, pausing asynchronously if rate limit is reached."""
        async with self._lock:
            now = time.monotonic()
            elapsed = now - self._last_updated
            self._last_updated = now

            # Replenish available request permits based on elapsed time (req/sec)
            self._available_requests = min(
                self.burst_capacity,
                self._available_requests + elapsed * self.requests_per_second,
            )

            if self._available_requests < 1.0:
                needed = 1.0 - self._available_requests
                wait_seconds = needed / self.requests_per_second
                await asyncio.sleep(wait_seconds)
                # After sleeping, exactly 1 request permit is consumed
                self._available_requests = 0.0
                self._last_updated = time.monotonic()
            else:
                self._available_requests -= 1.0


class NimAdapter(ModelAdapter):
    """Adapter for NVIDIA NIM models via OpenAI-compatible chat completions."""

    def __init__(
        self,
        model: str,
        *,
        api_key: str | None = None,
        base_url: str = DEFAULT_NIM_BASE_URL,
        timeout: float = DEFAULT_NIM_TIMEOUT_SECONDS,
        rate_limit_rps: float = DEFAULT_NIM_RATE_LIMIT_RPS,
        client: httpx.AsyncClient | None = None,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        """Initialize the NVIDIA NIM adapter.

        Args:
            model: Model identifier, e.g. 'meta/llama-3-70b-instruct'.
            api_key: NVIDIA NIM API key. If omitted, read from NVIDIA_API_KEY or NIM_API_KEY env.
            base_url: Provider base URL. Must start with https://.
            timeout: Explicit request timeout in seconds.
            rate_limit_rps: Max HTTP requests per second (req/sec, defaults to 40.0).
            client: Optional pre-configured httpx.AsyncClient (for testing).
            transport: Optional custom httpx transport (e.g. MockTransport for testing).
        """
        if not model or not model.strip():
            raise ValueError("Model identifier must not be empty")

        self.model = model.strip()
        self.base_url = validate_https_url(base_url)
        self.timeout = float(timeout)
        self.rate_limit_rps = float(rate_limit_rps)

        resolved_key = api_key or os.environ.get("NVIDIA_API_KEY") or os.environ.get("NIM_API_KEY")
        if not resolved_key or not resolved_key.strip():
            raise ValueError(
                "NVIDIA NIM API key is required. Set NVIDIA_API_KEY or NIM_API_KEY environment "
                + "variable, or pass api_key explicitly."
            )
        self._api_key = resolved_key.strip()

        # Request rate limiter setup (40 req/sec)
        self._rate_limiter = (
            AsyncRateLimiter(requests_per_second=self.rate_limit_rps)
            if self.rate_limit_rps > 0
            else None
        )

        # Client management
        self._owns_client = client is None
        if client is not None:
            self._client = client
        else:
            self._client = httpx.AsyncClient(
                base_url=self.base_url,
                timeout=httpx.Timeout(self.timeout),
                transport=transport,
            )

    @property
    def api_key(self) -> str:
        """Configured API key."""
        return self._api_key

    @override
    async def generate(
        self,
        messages: Sequence[Mapping[str, str]] | str,
        *,
        max_tokens: int | None = None,
    ) -> ModelReply:
        """Query the model with messages and return a ModelReply envelope."""
        # Normalize input to chat message format
        if isinstance(messages, str):
            formatted_messages: list[dict[str, str]] = [{"role": "user", "content": messages}]
        else:
            formatted_messages = [dict(m) for m in messages]

        payload: dict[str, object] = {
            "model": self.model,
            "messages": formatted_messages,
            "temperature": 0.0,
        }
        if max_tokens is not None:
            payload["max_tokens"] = max_tokens

        headers = {
            "Authorization": f"Bearer {self._api_key}",
            "Content-Type": "application/json",
            "Accept": "application/json",
        }

        # Apply client-side 40 req/s rate limiting before outbound HTTP request
        if self._rate_limiter is not None:
            await self._rate_limiter.acquire()

        t0 = time.perf_counter()
        try:
            response = await self._client.post(
                "/chat/completions",
                json=payload,
                headers=headers,
            )
            latency_ms = (time.perf_counter() - t0) * 1000.0

            # 429 Rate Limit
            if response.status_code == 429:
                retry_after = response.headers.get("retry-after")
                msg = f"{ERR_RATE_LIMIT}: 429 Too Many Requests"
                if retry_after:
                    msg += f" (retry-after={retry_after})"
                return ModelReply(
                    text=None,
                    input_tokens=0,
                    output_tokens=0,
                    latency_ms=latency_ms,
                    error=msg,
                )

            # 401/403 Authentication Error
            if response.status_code in (401, 403):
                return ModelReply(
                    text=None,
                    input_tokens=0,
                    output_tokens=0,
                    latency_ms=latency_ms,
                    error=f"{ERR_AUTH}: {response.status_code} {response.text.strip()}",
                )

            # Other HTTP errors
            if response.status_code >= 400:
                return ModelReply(
                    text=None,
                    input_tokens=0,
                    output_tokens=0,
                    latency_ms=latency_ms,
                    error=f"{ERR_HTTP}: {response.status_code} {response.text.strip()}",
                )

            # Parse JSON body
            try:
                data = response.json()
            except Exception as exc:
                return ModelReply(
                    text=None,
                    input_tokens=0,
                    output_tokens=0,
                    latency_ms=latency_ms,
                    error=f"{ERR_MALFORMED}: invalid json response body ({exc})",
                )

            # Extract completion text
            choices = data.get("choices")
            if not isinstance(choices, list) or not choices:
                return ModelReply(
                    text=None,
                    input_tokens=0,
                    output_tokens=0,
                    latency_ms=latency_ms,
                    error=f"{ERR_MALFORMED}: missing or empty 'choices' list in response",
                )

            first_choice = choices[0]
            message = first_choice.get("message") if isinstance(first_choice, dict) else None
            content = message.get("content") if isinstance(message, dict) else None
            if content is None or not isinstance(content, str):
                return ModelReply(
                    text=None,
                    input_tokens=0,
                    output_tokens=0,
                    latency_ms=latency_ms,
                    error=f"{ERR_MALFORMED}: missing or non-string 'content' in first choice message",
                )

            # Extract token counts
            usage = data.get("usage") or {}
            input_tokens = int(usage.get("prompt_tokens") or 0)
            output_tokens = int(usage.get("completion_tokens") or 0)

            return ModelReply(
                text=content,
                input_tokens=input_tokens,
                output_tokens=output_tokens,
                latency_ms=latency_ms,
                error=None,
            )

        except httpx.TimeoutException as exc:
            latency_ms = (time.perf_counter() - t0) * 1000.0
            return ModelReply(
                text=None,
                input_tokens=0,
                output_tokens=0,
                latency_ms=latency_ms,
                error=f"{ERR_TIMEOUT}: request timed out ({exc})",
            )
        except httpx.RequestError as exc:
            latency_ms = (time.perf_counter() - t0) * 1000.0
            return ModelReply(
                text=None,
                input_tokens=0,
                output_tokens=0,
                latency_ms=latency_ms,
                error=f"{ERR_NETWORK}: {exc}",
            )
        except Exception as exc:
            latency_ms = (time.perf_counter() - t0) * 1000.0
            return ModelReply(
                text=None,
                input_tokens=0,
                output_tokens=0,
                latency_ms=latency_ms,
                error=f"{ERR_HTTP}: unexpected error ({exc})",
            )

    @override
    async def close(self) -> None:
        """Close the underlying HTTP client if owned by this adapter."""
        if self._owns_client and not self._client.is_closed:
            await self._client.aclose()
