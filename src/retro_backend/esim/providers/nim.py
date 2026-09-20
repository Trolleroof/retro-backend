"""NVIDIA NIM provider adapter for E-Sim (#69).

Provides a pure httpx.AsyncClient implementation for interacting with NVIDIA NIM
hosted or self-hosted OpenAI-compatible endpoints (https://integrate.api.nvidia.com/v1).

Key design features:
- A rolling-window request budget shared by every adapter in the process
  (:mod:`retro_backend.esim.providers.budget`), so a managed run's manager,
  workers, and retries draw on one allowance instead of one each. Note that
  this bounds HTTP request starts, which is distinct from LLM input/output
  generation tokens.
- Strict HTTPS enforcement.
- Hardcoded temperature 0.0 for deterministic evaluation.
- Detailed token consumption and latency measurement on success and error paths.
- Resilient error handling translating network drops, timeouts, 4xx/5xx, and malformed
  responses into structured ModelReply envelopes without raising unhandled exceptions.
"""

import os
import time
from collections.abc import Mapping, Sequence
from dataclasses import replace
from typing import override

import httpx

from retro_backend.esim.providers.base import (
    ERR_AUTH,
    ERR_BUDGET_TIMEOUT,
    ERR_HTTP,
    ERR_NETWORK,
    ERR_PROVIDER_MALFORMED,
    ERR_PROVIDER_UNAVAILABLE,
    ERR_RATE_LIMIT,
    ERR_TIMEOUT,
    ERR_TRUNCATED,
    ModelAdapter,
    ModelReply,
    validate_https_url,
)
from retro_backend.esim.providers.budget import (
    AdmissionTimeout,
    RollingWindowBudget,
    default_budget,
)

__all__ = [
    "DEFAULT_NIM_BASE_URL",
    "DEFAULT_NIM_TIMEOUT_SECONDS",
    "NimAdapter",
    "parse_retry_after",
]

DEFAULT_NIM_BASE_URL = "https://integrate.api.nvidia.com/v1"
DEFAULT_NIM_TIMEOUT_SECONDS = 30.0


def parse_retry_after(value: str | None) -> float | None:
    """Read a Retry-After header as seconds, or None when it says nothing usable.

    Only the delta-seconds form is honored. The HTTP-date form is legal and
    NIM has not been observed to send it; treating an unparsable header as
    "no guidance" leaves the budget's own window in charge, which is the safe
    direction to fail.
    """
    if value is None:
        return None
    try:
        seconds = float(value.strip())
    except ValueError:
        return None
    return seconds if seconds > 0 else None


class NimAdapter(ModelAdapter):
    """Adapter for NVIDIA NIM models via OpenAI-compatible chat completions."""

    def __init__(
        self,
        model: str,
        *,
        api_key: str | None = None,
        base_url: str = DEFAULT_NIM_BASE_URL,
        timeout: float = DEFAULT_NIM_TIMEOUT_SECONDS,
        budget: RollingWindowBudget | None = None,
        client: httpx.AsyncClient | None = None,
        transport: httpx.AsyncBaseTransport | None = None,
        request_options: Mapping[str, object] | None = None,
    ) -> None:
        """Initialize the NVIDIA NIM adapter.

        Args:
            model: Model identifier, e.g. 'meta/llama-3-70b-instruct'.
            api_key: NVIDIA NIM API key. If omitted, read from NVIDIA_API_KEY or NIM_API_KEY env.
            base_url: Provider base URL. Must start with https://.
            timeout: Explicit request timeout in seconds.
            budget: The rolling-window request budget to draw on. Defaults to
                the process-wide one, which is the point: two adapters built
                for one run must share an allowance, not hold one each. Pass
                an explicit budget only to isolate a test.
            client: Optional pre-configured httpx.AsyncClient (for testing).
            transport: Optional custom httpx transport (e.g. MockTransport for testing).
        """
        if not model or not model.strip():
            raise ValueError("Model identifier must not be empty")

        self.model = model.strip()
        self.base_url = validate_https_url(base_url)
        self.timeout = float(timeout)
        self._budget = budget if budget is not None else default_budget()

        resolved_key = api_key or os.environ.get("NVIDIA_API_KEY") or os.environ.get("NIM_API_KEY")
        if not resolved_key or not resolved_key.strip():
            raise ValueError(
                "NVIDIA NIM API key is required. Set NVIDIA_API_KEY or NIM_API_KEY environment "
                + "variable, or pass api_key explicitly."
            )
        self._api_key = resolved_key.strip()
        self._request_options = dict(request_options or {})

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

    @property
    def budget(self) -> RollingWindowBudget:
        """The request budget this adapter draws on.

        Public so a caller can read :meth:`RollingWindowBudget.snapshot` for a
        waiting indicator, and so a test can assert that two adapters share
        one allowance rather than holding one each.
        """
        return self._budget

    @override
    async def generate(
        self,
        messages: Sequence[Mapping[str, str]] | str,
        *,
        max_tokens: int | None = None,
    ) -> ModelReply:
        """Query the model with messages and return a ModelReply envelope.

        Admission comes first and is awaited outside the latency measurement:
        time spent queued for the account's budget is not time the model spent
        answering, and folding the two together would make a throttled run look
        like a slow model.
        """
        t0 = time.perf_counter()
        try:
            queue_delay_seconds = await self._budget.acquire()
        except AdmissionTimeout as exc:
            # Reported rather than raised, like every other failure here, and
            # with the wait it did pay recorded: a turn lost to this process'
            # own rationing must be legible as that, not as a provider fault.
            return ModelReply(
                text=None,
                input_tokens=0,
                output_tokens=0,
                latency_ms=0.0,
                error=f"{ERR_BUDGET_TIMEOUT}: {exc}",
                queue_delay_ms=(time.perf_counter() - t0) * 1000.0,
            )
        reply = await self._request(messages, max_tokens=max_tokens)
        return replace(reply, queue_delay_ms=queue_delay_seconds * 1000.0)

    async def _request(
        self,
        messages: Sequence[Mapping[str, str]] | str,
        *,
        max_tokens: int | None = None,
    ) -> ModelReply:
        """Issue one already-admitted request and envelope whatever comes back."""
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
        payload.update(self._request_options)

        headers = {
            "Authorization": f"Bearer {self._api_key}",
            "Content-Type": "application/json",
            "Accept": "application/json",
        }

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
                # The account said stop, so every caller in this process stops,
                # not just this one. A 429 means the window this budget models
                # is wrong right now; the next caller learning that the hard way
                # too would spend a second request to find out the same thing.
                seconds = parse_retry_after(retry_after)
                if seconds is not None:
                    applied = self._budget.penalize(seconds)
                    msg += f" (backing off {applied:.0f}s)"
                return ModelReply(
                    text=None,
                    input_tokens=0,
                    output_tokens=0,
                    latency_ms=latency_ms,
                    error=msg,
                    retry_after_seconds=parse_retry_after(retry_after),
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
                error_prefix = ERR_PROVIDER_UNAVAILABLE if response.status_code == 503 else ERR_HTTP
                return ModelReply(
                    text=None,
                    input_tokens=0,
                    output_tokens=0,
                    latency_ms=latency_ms,
                    error=f"{error_prefix}: {response.status_code} {response.text.strip()}",
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
                    error=f"{ERR_PROVIDER_MALFORMED}: invalid json response body ({exc})",
                )

            # Extract completion text
            choices = data.get("choices")
            if not isinstance(choices, list) or not choices:
                return ModelReply(
                    text=None,
                    input_tokens=0,
                    output_tokens=0,
                    latency_ms=latency_ms,
                    error=f"{ERR_PROVIDER_MALFORMED}: missing or empty 'choices' list in response",
                )

            # Token counts and the answering model are read before any early
            # return. A truncated call still consumed the whole budget and
            # still costs money, so reporting zero would understate real
            # usage. The model id matters for the same reason in reverse: if
            # the provider served a different model on the attempt that
            # failed, dropping the id there lets a run look like the
            # single-model run the manifest claims while a second model
            # actually shaped its retries.
            usage = data.get("usage") or {}
            input_tokens = int(usage.get("prompt_tokens") or 0)
            output_tokens = int(usage.get("completion_tokens") or 0)
            completion_details = usage.get("completion_tokens_details") or {}
            reasoning_tokens = int(
                (completion_details.get("reasoning_tokens") if isinstance(completion_details, dict) else 0)
                or usage.get("reasoning_tokens")
                or 0
            )
            raw_served = data.get("model")
            served = raw_served if isinstance(raw_served, str) else None

            first_choice = choices[0]
            message = first_choice.get("message") if isinstance(first_choice, dict) else None
            content = message.get("content") if isinstance(message, dict) else None
            raw_reasoning = (
                message.get("reasoning_content")
                or message.get("reasoning")
                or message.get("reasoning_details")
                if isinstance(message, dict)
                else None
            )
            reasoning = raw_reasoning if isinstance(raw_reasoning, str) else None
            finish = first_choice.get("finish_reason") if isinstance(first_choice, dict) else None

            # Truncation is decided by finish_reason, not by what content looks
            # like. The budget runs out in two different-looking ways: a
            # reasoning model leaves content null, having spent everything
            # thinking, while a model writing directly leaves a half-written
            # string. The second parses as "invalid json", which reads as a
            # model that cannot follow the format and, being retryable, spends
            # two more calls truncating identically. Both are one cause.
            if finish == "length":
                reasoned = isinstance(message, dict) and bool(
                    message.get("reasoning_content") or message.get("reasoning")
                )
                where = "before any answer was written" if not content else "mid-answer"
                detail = f"output token budget exhausted {where}" + (
                    " (spent on reasoning tokens)" if reasoned else ""
                )
                return ModelReply(
                    text=None,
                    input_tokens=input_tokens,
                    output_tokens=output_tokens,
                    latency_ms=latency_ms,
                    error=f"{ERR_TRUNCATED}: {detail}",
                    served_model=served,
                    reasoning=reasoning,
                    reasoning_tokens=reasoning_tokens,
                )

            if content is None or not isinstance(content, str):
                return ModelReply(
                    text=None,
                    input_tokens=input_tokens,
                    output_tokens=output_tokens,
                    latency_ms=latency_ms,
                    error=f"{ERR_PROVIDER_MALFORMED}: missing or non-string 'content' in first choice message",
                    served_model=served,
                    reasoning=reasoning,
                    reasoning_tokens=reasoning_tokens,
                )

            return ModelReply(
                text=content,
                input_tokens=input_tokens,
                output_tokens=output_tokens,
                latency_ms=latency_ms,
                error=None,
                served_model=served,
                reasoning=reasoning,
                reasoning_tokens=reasoning_tokens,
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
