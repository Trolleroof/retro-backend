"""NVIDIA NIM provider adapter for E-Sim (#69).

Provides a pure httpx.AsyncClient implementation for interacting with NVIDIA NIM
hosted or self-hosted OpenAI-compatible endpoints (https://integrate.api.nvidia.com/v1).

Key design features:
- Explicit 40 requests/sec token-bucket rate limiter to avoid 429 Too Many Requests.
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
DEFAULT_NIM_RATE_LIMIT_RPS = 40.0


class AsyncRateLimiter:
    """Thread-safe and async-safe token bucket rate limiter.

    Replenishes tokens smoothly based on elapsed monotonic time up to a maximum
    burst capacity. Callers await :meth:`acquire` before issuing outbound requests.
    """

    def __init__(self, rate: float = 40.0, capacity: float | None = None) -> None:
        if rate <= 0:
            raise ValueError(f"Rate must be strictly positive, got: {rate}")
        self.rate = float(rate)
        self.capacity = float(capacity if capacity is not None else rate)
        self._tokens = self.capacity
        self._last_updated = time.monotonic()
        self._lock = asyncio.Lock()

    async def acquire(self) -> None:
        """Acquire a single request permit, pausing asynchronously if necessary."""
        async with self._lock:
            now = time.monotonic()
            elapsed = now - self._last_updated
            self._last_updated = now

            # Replenish tokens based on elapsed time
            self._tokens = min(self.capacity, self._tokens + elapsed * self.rate)

            if self._tokens < 1.0:
                needed = 1.0 - self._tokens
                wait_seconds = needed / self.rate
                await asyncio.sleep(wait_seconds)
                # After sleeping, tokens have reached 1.0
                self._tokens = 0.0
                self._last_updated = time.monotonic()
            else:
                self._tokens -= 1.0


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
            rate_limit_rps: Target max requests per second (defaults to 40.0).
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

        # Rate limiter setup
        self._rate_limiter = (
            AsyncRateLimiter(rate=self.rate_limit_rps) if self.rate_limit_rps > 0 else None
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

        # Apply client-side 40 req/s rate limiting
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
