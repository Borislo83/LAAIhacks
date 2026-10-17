"""Thin ASI:One client (OpenAI-compatible chat completions).

Reads ASI_ONE_API_KEY from the environment. Without a key every call returns
None and callers fall back to plain-code behaviour.
"""

import os

import aiohttp

ASI_ONE_URL = "https://api.asi1.ai/v1/chat/completions"
ASI_ONE_MODEL = os.getenv("ASI_ONE_MODEL", "asi1-mini")


def has_key() -> bool:
    return bool(os.getenv("ASI_ONE_API_KEY"))


async def complete(system: str, user: str, timeout: float = 30.0) -> str | None:
    key = os.getenv("ASI_ONE_API_KEY")
    if not key:
        return None
    payload = {
        "model": ASI_ONE_MODEL,
        "messages": [
            {"role": "system", "content": system},
            {"role": "user", "content": user},
        ],
    }
    headers = {"Authorization": f"Bearer {key}", "Content-Type": "application/json"}
    try:
        async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=timeout)) as session:
            async with session.post(ASI_ONE_URL, json=payload, headers=headers) as resp:
                resp.raise_for_status()
                data = await resp.json()
        return data["choices"][0]["message"]["content"].strip()
    except (aiohttp.ClientError, TimeoutError, KeyError, IndexError):
        return None


async def extract_product(text: str) -> str:
    """Pull the product name out of a natural-language request."""
    answer = await complete(
        "Extract the single product name the user is asking about. "
        "Reply with only the product name, nothing else.",
        text,
    )
    return answer or text
