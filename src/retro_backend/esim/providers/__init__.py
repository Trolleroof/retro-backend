"""Model provider adapters and registry for E-Sim (#69)."""

from retro_backend.esim.providers.base import (
    ERR_AUTH,
    ERR_BUDGET_TIMEOUT,
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
from retro_backend.esim.providers.budget import (
    DEFAULT_MAX_QUEUE_SECONDS,
    DEFAULT_MAX_STARTS_PER_WINDOW,
    DEFAULT_WINDOW_SECONDS,
    MAX_RETRY_AFTER_SECONDS,
    AdmissionTimeout,
    BudgetSnapshot,
    RollingWindowBudget,
    default_budget,
    reset_default_budget,
)
from retro_backend.esim.providers.nim import (
    DEFAULT_NIM_BASE_URL,
    DEFAULT_NIM_TIMEOUT_SECONDS,
    NimAdapter,
    parse_retry_after,
)
from retro_backend.esim.providers.registry import (
    get_adapter,
    parse_model_spec,
    register_provider,
)

__all__ = [
    "DEFAULT_MAX_QUEUE_SECONDS",
    "DEFAULT_MAX_STARTS_PER_WINDOW",
    "DEFAULT_NIM_BASE_URL",
    "DEFAULT_NIM_TIMEOUT_SECONDS",
    "DEFAULT_WINDOW_SECONDS",
    "ERR_AUTH",
    "ERR_BUDGET_TIMEOUT",
    "ERR_CONFIGURATION",
    "ERR_HTTP",
    "ERR_MALFORMED",
    "ERR_NETWORK",
    "ERR_RATE_LIMIT",
    "ERR_TIMEOUT",
    "MAX_RETRY_AFTER_SECONDS",
    "AdmissionTimeout",
    "BudgetSnapshot",
    "ModelAdapter",
    "ModelReply",
    "NimAdapter",
    "RollingWindowBudget",
    "default_budget",
    "get_adapter",
    "parse_model_spec",
    "parse_retry_after",
    "register_provider",
    "reset_default_budget",
    "validate_https_url",
]
