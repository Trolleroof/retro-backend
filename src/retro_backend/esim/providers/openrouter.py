"""OpenRouter adapter using its OpenAI-compatible chat-completions API."""

import os

import httpx

from retro_backend.esim.providers.budget import RollingWindowBudget
from retro_backend.esim.providers.nim import NimAdapter

__all__ = ["DEFAULT_OPENROUTER_BASE_URL", "OpenRouterAdapter"]

DEFAULT_OPENROUTER_BASE_URL = "https://openrouter.ai/api/v1"


class OpenRouterAdapter(NimAdapter):
    """Reuse the compatible transport while resolving OpenRouter credentials."""

    def __init__(
        self,
        model: str,
        *,
        api_key: str | None = None,
        timeout: float = 30.0,
        budget: RollingWindowBudget | None = None,
        client: httpx.AsyncClient | None = None,
        transport: httpx.AsyncBaseTransport | None = None,
        provider_order: tuple[str, ...] = (),
        reasoning_effort: str | None = None,
    ) -> None:
        resolved_key = api_key or os.environ.get("OPENROUTER_API_KEY")
        if not resolved_key or not resolved_key.strip():
            raise ValueError("OpenRouter API key is required. Set OPENROUTER_API_KEY or pass api_key explicitly.")
        provider: dict[str, object] = {"allow_fallbacks": False}
        if provider_order:
            provider["order"] = list(provider_order)
        request_options: dict[str, object] = {"provider": provider}
        if reasoning_effort:
            request_options["reasoning_effort"] = reasoning_effort
        super().__init__(
            model,
            api_key=resolved_key,
            base_url=DEFAULT_OPENROUTER_BASE_URL,
            timeout=timeout,
            budget=budget,
            client=client,
            transport=transport,
            request_options=request_options,
        )
