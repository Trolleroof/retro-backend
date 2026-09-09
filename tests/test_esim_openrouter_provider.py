"""OpenRouter uses the existing OpenAI-compatible E-Sim transport."""

import httpx
import pytest

from retro_backend.esim.providers import OpenRouterAdapter, RollingWindowBudget, get_adapter


@pytest.mark.asyncio
async def test_openrouter_adapter_uses_openrouter_endpoint_and_key() -> None:
    seen: dict[str, object] = {}

    async def handler(request: httpx.Request) -> httpx.Response:
        seen["url"] = str(request.url)
        seen["auth"] = request.headers["authorization"]
        seen["body"] = request.json() if hasattr(request, "json") else request.content
        return httpx.Response(200, json={"model": "moonshotai/kimi-k3", "choices": [{"message": {"content": "{\"action\":\"wait\"}"}}], "usage": {"prompt_tokens": 2, "completion_tokens": 3}})

    adapter = OpenRouterAdapter(
        "moonshotai/kimi-k3",
        api_key="test-key",
        transport=httpx.MockTransport(handler),
        budget=RollingWindowBudget(max_starts=1),
    )
    try:
        reply = await adapter.generate("hello")
    finally:
        await adapter.close()

    assert seen["url"] == "https://openrouter.ai/api/v1/chat/completions"
    assert seen["auth"] == "Bearer test-key"
    assert reply.served_model == "moonshotai/kimi-k3"
    assert reply.text == '{"action":"wait"}'


def test_registry_resolves_openrouter(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("OPENROUTER_API_KEY", "test-key")
    assert isinstance(get_adapter("openrouter:moonshotai/kimi-k3"), OpenRouterAdapter)
