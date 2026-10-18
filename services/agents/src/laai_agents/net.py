"""One shared aiohttp session per event loop, closed on agent shutdown."""

import asyncio

import aiohttp

_shared: tuple[asyncio.AbstractEventLoop, aiohttp.ClientSession] | None = None


def session() -> aiohttp.ClientSession:
    """Callers pass their own timeout and headers per request."""
    global _shared
    loop = asyncio.get_running_loop()
    if _shared is None or _shared[0] is not loop or _shared[1].closed:
        # No cookie jar: merchant pages and APIs must not share cookies.
        _shared = (loop, aiohttp.ClientSession(cookie_jar=aiohttp.DummyCookieJar()))
    return _shared[1]


async def close() -> None:
    global _shared
    if _shared is not None:
        loop, current = _shared
        _shared = None
        if loop is asyncio.get_running_loop():
            await current.close()
