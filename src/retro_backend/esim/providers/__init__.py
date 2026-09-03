"""Model provider adapters and registry for E-Sim (#69)."""

from retro_backend.esim.providers.base import (
    ERR_AUTH,
    ERR_CONFIGURATION,
    ERR_HTTP,
    ERR_MALFORMED,
    ERR_NETWORK,
    ERR_RATE_LIMIT,
    ERR_TIMEOUT,
    ModelAdapter,
    ModelReply,
    validate_https_url,
)
from retro_backend.esim.providers.nim import (
    DEFAULT_NIM_BASE_URL,
    DEFAULT_NIM_RATE_LIMIT_RPS,
    DEFAULT_NIM_TIMEOUT_SECONDS,
    AsyncRateLimiter,
    NimAdapter,
)
from retro_backend.esim.providers.registry import (
    get_adapter,
    parse_model_spec,
    register_provider,
)

__all__ = [
    "DEFAULT_NIM_BASE_URL",
    "DEFAULT_NIM_RATE_LIMIT_RPS",
    "DEFAULT_NIM_TIMEOUT_SECONDS",
    "ERR_AUTH",
    "ERR_CONFIGURATION",
    "ERR_HTTP",
    "ERR_MALFORMED",
    "ERR_NETWORK",
    "ERR_RATE_LIMIT",
    "ERR_TIMEOUT",
    "AsyncRateLimiter",
    "ModelAdapter",
    "ModelReply",
    "NimAdapter",
    "get_adapter",
    "parse_model_spec",
    "register_provider",
    "validate_https_url",
]
