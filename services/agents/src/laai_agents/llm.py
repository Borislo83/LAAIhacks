"""Thin ASI:One client (OpenAI-compatible chat completions).

Reads ASI_ONE_API_KEY from the environment. Without a key every call returns
None and callers fall back to plain-code behaviour.
"""

import aiohttp

from . import asi, net


def has_key() -> bool:
    return bool(asi.api_key())


async def complete(system: str, user: str, timeout: float = 30.0) -> str | None:
    key = asi.api_key()
    if not key:
        return None
    payload = {
        "model": asi.model(),
        "messages": [
            {"role": "system", "content": system},
            {"role": "user", "content": user},
        ],
    }
    try:
        async with net.session().post(
            asi.BASE_URL + "/chat/completions",
            json=payload,
            headers=asi.headers(key),
            timeout=aiohttp.ClientTimeout(total=timeout),
        ) as resp:
            resp.raise_for_status()
            data = await resp.json()
        return data["choices"][0]["message"]["content"].strip()
    # A malformed answer (no text, not JSON, not an object) is no answer, not a crash.
    except (aiohttp.ClientError, TimeoutError, KeyError, IndexError, TypeError,
            AttributeError, ValueError):
        return None


async def extract_product(text: str) -> str:
    """Pull the product name out of a natural-language request."""
    answer = await complete(
        "Extract the single product name the user is asking about. "
        "Reply with only the product name, nothing else.",
        text,
    )
    return answer or text
