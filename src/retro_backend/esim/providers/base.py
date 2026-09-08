"""Core contracts and protocols for E-Sim model provider adapters (#69).

This module defines the protocol and data structures for interacting with
large language model backends. Providers must implement :class:`ModelAdapter`
and return a :class:`ModelReply` capturing output text, token counts, latency,
and any typed error details without raising unhandled runtime exceptions.
"""

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Protocol, Self, runtime_checkable

__all__ = [
    "ERR_AUTH",
    "ERR_CONFIGURATION",
    "ERR_HTTP",
    "ERR_MALFORMED",
    "ERR_NETWORK",
    "ERR_PROVIDER_UNAVAILABLE",
    "ERR_RATE_LIMIT",
    "ERR_TIMEOUT",
    "ERR_TRUNCATED",
    "ModelAdapter",
    "ModelReply",
    "validate_https_url",
]

# Standard error category prefixes
ERR_AUTH = "auth_error"
ERR_CONFIGURATION = "configuration_error"
ERR_HTTP = "http_error"
ERR_MALFORMED = "malformed_response"
ERR_NETWORK = "network_error"
ERR_PROVIDER_UNAVAILABLE = "provider_unavailable"
ERR_RATE_LIMIT = "rate_limit"
ERR_TIMEOUT = "timeout"
# The budget ran out before the model wrote an answer. Kept distinct from
# ERR_MALFORMED because it is not a provider fault: an unchanged retry under
# the same budget would truncate the same way. The runner's retry is not
# unchanged, though -- it folds this error into the next attempt's prompt as
# previousError, asking for a shorter answer -- so this is retryable in
# rotation.retryable_model_error on that basis. If a persistently truncating
# model needs more than a prompt nudge, raise max_output_tokens.
ERR_TRUNCATED = "truncated_response"


def validate_https_url(url: str) -> str:
    """Ensure a provider base URL strictly uses the HTTPS scheme.

    Raises:
        ValueError: If the URL scheme is not https.
    """
    stripped = url.strip()
    if not stripped.lower().startswith("https://"):
        raise ValueError(
            f"Provider URL must use HTTPS scheme to prevent insecure credential transmission; got: {url!r}"
        )
    return stripped.rstrip("/")


@dataclass(frozen=True, slots=True)
class ModelReply:
    """Standard reply envelope returned by all model adapters.

    Attributes:
        text: The generated completion text, or None if the request failed.
        input_tokens: Number of prompt/input tokens consumed.
        output_tokens: Number of generated completion tokens consumed.
        latency_ms: Roundtrip wall-clock latency in milliseconds.
        error: Typed error description if call failed, None otherwise.
        served_model: The model id the provider says answered, read back from
            the response body rather than echoed from the request. A benchmark
            claims "model X scored Y", and that claim is only auditable if the
            run recorded which model actually replied: a provider is free to
            route a deprecated or aliased id somewhere else. ``None`` when the
            call failed or the body carried no id.
    """

    text: str | None
    input_tokens: int
    output_tokens: int
    latency_ms: float
    error: str | None = None
    served_model: str | None = None
    retry_after_seconds: float | None = None

    @property
    def ok(self) -> bool:
        """True if the model call succeeded without error."""
        return self.error is None and self.text is not None


@runtime_checkable
class ModelAdapter(Protocol):
    """Protocol satisfied by all LLM provider adapters."""

    async def generate(
        self,
        messages: Sequence[Mapping[str, str]] | str,
        *,
        max_tokens: int | None = None,
    ) -> ModelReply:
        """Generate a response for the provided prompt or chat messages.

        Args:
            messages: Either a single prompt string or a sequence of chat message dicts
                containing 'role' and 'content' keys.
            max_tokens: Optional limit on the number of tokens generated.

        Returns:
            A :class:`ModelReply` containing the response text, token counts, latency,
            and error information. Never raises an unhandled HTTP or network exception.
        """
        ...

    async def close(self) -> None:
        """Close underlying HTTP client resources."""
        ...

    async def __aenter__(self) -> Self:
        return self

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc_val: BaseException | None,
        exc_tb: object | None,
    ) -> None:
        await self.close()
