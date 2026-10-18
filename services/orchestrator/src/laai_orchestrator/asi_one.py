"""The one ASI:One chat-completions call shared by chat and page prediction."""

from typing import Any

import httpx

from .config import Config

ASI_ONE_URL = "https://api.asi1.ai/v1/chat/completions"


async def complete(
    messages: list[dict[str, Any]],
    config: Config,
    http: httpx.AsyncClient,
    *,
    max_tokens: int,
    timeout: float,
    tools: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    """Return the first choice's message; raises httpx/KeyError/IndexError on bad replies."""
    body: dict[str, Any] = {
        "model": config.asi_one_model,
        "messages": messages,
        "temperature": 0,
        "max_tokens": max_tokens,
    }
    if tools:
        body["tools"] = tools
    response = await http.post(
        ASI_ONE_URL,
        headers={"Authorization": f"Bearer {config.asi_one_api_key.get_secret_value()}"},
        json=body,
        timeout=timeout,
    )
    response.raise_for_status()
    return response.json()["choices"][0]["message"]
