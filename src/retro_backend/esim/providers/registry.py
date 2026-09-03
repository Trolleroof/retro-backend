"""Model spec parser and provider adapter registry for E-Sim (#69).

Parses model specifications of the form '<provider>:<model>' (e.g.,
'nim:meta/llama-3-70b-instruct') and instantiates configured adapters.
"""

from collections.abc import Callable
from typing import Any

from retro_backend.esim.providers.base import ModelAdapter
from retro_backend.esim.providers.nim import NimAdapter

__all__ = [
    "get_adapter",
    "parse_model_spec",
    "register_provider",
]


def _create_nim_adapter(model: str, **kwargs: Any) -> ModelAdapter:
    return NimAdapter(model=model, **kwargs)


_PROVIDER_FACTORIES: dict[str, Callable[..., ModelAdapter]] = {
    "nim": _create_nim_adapter,
}


def parse_model_spec(spec: str) -> tuple[str, str]:
    """Parse a provider model specification into (provider, model_name).

    Args:
        spec: String formatted as '<provider>:<model>', e.g. 'nim:meta/llama-3-70b-instruct'.

    Returns:
        A tuple of (provider, model_name).

    Raises:
        ValueError: If the spec does not conform to '<provider>:<model>'.
    """
    cleaned = spec.strip()
    if ":" not in cleaned:
        raise ValueError(
            f"Invalid model spec: {spec!r}. Expected format: '<provider>:<model>' "
            + "(e.g. 'nim:meta/llama-3-70b-instruct')"
        )
    provider, model = cleaned.split(":", 1)
    provider = provider.strip().lower()
    model = model.strip()
    if not provider or not model:
        raise ValueError(
            f"Invalid model spec: {spec!r}. Both provider and model must be non-empty."
        )
    return provider, model


def register_provider(name: str, factory: Callable[..., ModelAdapter]) -> None:
    """Register or override a provider factory in the global registry."""
    _PROVIDER_FACTORIES[name.strip().lower()] = factory


def get_adapter(spec: str, **kwargs: Any) -> ModelAdapter:
    """Resolve a model specification and return an initialized ModelAdapter.

    Args:
        spec: Model spec string, e.g. 'nim:meta/llama-3-70b-instruct'.
        **kwargs: Additional configuration parameters passed to the provider factory
            (e.g., api_key, timeout, rate_limit_rps, base_url, transport).

    Returns:
        An initialized :class:`ModelAdapter`.

    Raises:
        ValueError: If the model spec is invalid or the provider is unsupported.
    """
    provider, model = parse_model_spec(spec)
    factory = _PROVIDER_FACTORIES.get(provider)
    if factory is None:
        supported = ", ".join(sorted(_PROVIDER_FACTORIES.keys()))
        raise ValueError(
            f"Unsupported provider {provider!r} in spec {spec!r}. "
            + f"Currently supported providers: {supported}"
        )
    return factory(model, **kwargs)
