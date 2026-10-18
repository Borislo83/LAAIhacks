"""ASI:One settings shared by the LLM client and web search, read at call time."""

import os

BASE_URL = "https://api.asi1.ai/v1"


def api_key() -> str:
    return os.getenv("ASI_ONE_API_KEY", "")


def model() -> str:
    return os.getenv("ASI_ONE_MODEL", "asi1-mini")


def headers(key: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {key}", "Content-Type": "application/json"}
