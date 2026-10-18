"""Online evidence, never a silent fallback to the demo catalog.

ASI search is accepted only when it reports a completed search and citations.
DDGS is a keyless fallback. Search snippets are leads, not verified checkout prices.
No destination returned by either provider is fetched by this service.
"""

import asyncio
import os
import time
from collections import OrderedDict
from datetime import UTC, datetime
from urllib.parse import urlsplit

import aiohttp

from . import asi, net
from .models import Source
from .progress import report


class SearchUnavailable(RuntimeError):
    pass


def https_url(value: str) -> bool:
    try:
        parsed = urlsplit(value)
        return (
            parsed.scheme == "https" and bool(parsed.hostname) and not parsed.username
        )
    except ValueError:
        return False


async def asi_search(query: str) -> list[Source]:
    key = asi.api_key()
    if not key:
        raise SearchUnavailable("ASI:One API key is not configured")
    async with net.session().post(
        asi.BASE_URL + "/responses",
        headers=asi.headers(key),
        timeout=aiohttp.ClientTimeout(total=35),
        json={
            "model": asi.model(),
            "input": query + " Search the web now and cite sources. Do not use memory.",
            "tools": [{"type": "web_search", "search_context_size": "medium"}],
        },
    ) as response:
        if response.status != 200:
            raise SearchUnavailable(f"ASI search returned HTTP {response.status}")
        data = await response.json()
    output = data.get("output", [])
    if not any(
        x.get("type") == "web_search_call" and x.get("status") == "completed"
        for x in output
    ):
        raise SearchUnavailable("ASI did not report a completed web search")
    sources = []
    for item in output:
        for content in item.get("content", []):
            for annotation in content.get("annotations", []):
                url = annotation.get("url", "")
                if annotation.get("type") == "url_citation" and https_url(url):
                    # A model's prose is not a verbatim source snippet.
                    sources.append(
                        Source(title=annotation.get("title") or url, url=url)
                    )
    if not sources:
        raise SearchUnavailable("ASI search did not return usable citations")
    return sources[:8]


# Public search rate-limits bursts, so queries are cached briefly and run two at a time.
_CACHE: OrderedDict[str, tuple[float, list[Source]]] = OrderedDict()
CACHE_SECONDS = 600
CACHE_SIZE = 256
_SLOTS = asyncio.Semaphore(2)
BACKENDS = ("yahoo,duckduckgo", "google,startpage", "auto")


def _cached(query: str) -> list[Source] | None:
    now = time.monotonic()
    # Insertion order is age order, so expired entries sit at the front.
    while _CACHE and now - next(iter(_CACHE.values()))[0] >= CACHE_SECONDS:
        _CACHE.popitem(last=False)
    hit = _CACHE.get(query)
    return hit[1] if hit else None


def _remember(query: str, sources: list[Source]) -> None:
    _CACHE.pop(query, None)
    _CACHE[query] = (time.monotonic(), sources)
    while len(_CACHE) > CACHE_SIZE:
        _CACHE.popitem(last=False)


async def free_search(query: str) -> list[Source]:
    if (cached := _cached(query)) is not None:
        return cached

    def run(backend):
        from ddgs import DDGS

        return DDGS(timeout=8).text(
            query, region="us-en", max_results=12, backend=backend
        )

    rows = []
    async with _SLOTS:
        for attempt, backend in enumerate(BACKENDS):
            try:
                rows = await asyncio.wait_for(asyncio.to_thread(run, backend), timeout=20)
            except Exception:  # noqa: BLE001 -- any provider failure means try the next one
                rows = []
            if rows:
                break
            await asyncio.sleep(0.5 * (attempt + 1))
    sources = [
        Source(
            title=str(row.get("title", "")),
            url=row["href"],
            snippet=str(row.get("body", "")),
        )
        for row in rows
        if https_url(row.get("href", ""))
    ]
    if not sources:
        raise SearchUnavailable("Free search is temporarily unavailable")
    _remember(query, sources)
    return sources


ASI_FAILURES = (TimeoutError, SearchUnavailable, aiohttp.ClientError, ValueError)


async def search(query: str) -> tuple[list[Source], dict]:
    report("searching", f"Searching online: {query}")
    provider = os.getenv("SEARCH_PROVIDER", "auto")
    if provider not in {"auto", "asi", "ddgs"}:
        raise SearchUnavailable("SEARCH_PROVIDER must be auto, asi, or ddgs")
    notice = "Search results are leads; verify price, availability and product variant at the source."
    if provider == "asi":
        try:
            sources = await asi_search(query)
        except ASI_FAILURES:
            raise SearchUnavailable(
                "ASI search unavailable; choose auto or ddgs for fallback"
            )
        provider = "asi_web_search"
    elif provider == "auto":
        # Seek verbatim snippets for numerical claims even if ASI finds citations.
        cited, snippets = await asyncio.gather(
            asi_search(query), free_search(query), return_exceptions=True
        )
        for result in (cited, snippets):
            if isinstance(result, BaseException) and not isinstance(result, ASI_FAILURES):
                raise result
        if isinstance(cited, BaseException):
            if isinstance(snippets, BaseException):
                raise snippets
            report("fallback", "ASI search unavailable; used free search")
            sources, provider = snippets, "ddgs"
            notice += " ASI search unavailable; used the free search fallback."
        elif isinstance(snippets, BaseException):
            sources, provider = cited, "asi_web_search"
        else:
            seen = {s.url for s in snippets}
            sources = snippets + [s for s in cited if s.url not in seen]
            provider = "asi_web_search+ddgs"
    else:
        sources = await free_search(query)
    report("sources_found", f"Found {len(sources)} online sources")
    return sources, {
        "data_source": provider,
        "fetched_at": datetime.now(UTC).isoformat(),
        "sources": sources,
        "notice": notice,
    }
