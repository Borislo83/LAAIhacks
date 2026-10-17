"""Online evidence, never a silent fallback to the demo catalog.

ASI search is accepted only when it reports a completed search and citations.
DDGS is a keyless fallback. Search snippets are leads, not verified checkout prices.
No destination returned by either provider is fetched by this service.
"""

import asyncio
import os
from datetime import UTC, datetime
from urllib.parse import urlsplit

import aiohttp

from .models import Source


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
    key = os.getenv("ASI_ONE_API_KEY", "")
    if not key:
        raise SearchUnavailable("ASI:One API key is not configured")
    async with (
        aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=35)) as session,
        session.post(
            "https://api.asi1.ai/v1/responses",
            headers={"Authorization": f"Bearer {key}"},
            json={
                "model": os.getenv("ASI_ONE_MODEL", "asi1-mini"),
                "input": query
                + " Search the web now and cite sources. Do not use memory.",
                "tools": [{"type": "web_search", "search_context_size": "medium"}],
            },
        ) as response,
    ):
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


async def free_search(query: str) -> list[Source]:
    def run():
        from ddgs import DDGS

        return DDGS(timeout=8).text(
            query, region="us-en", max_results=12, backend="yahoo,duckduckgo"
        )

    try:
        rows = await asyncio.wait_for(asyncio.to_thread(run), timeout=25)
    except Exception as exc:
        raise SearchUnavailable("Free search is temporarily unavailable") from exc
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
        raise SearchUnavailable("Free search returned no usable sources")
    return sources


async def search(query: str) -> tuple[list[Source], dict]:
    provider = os.getenv("SEARCH_PROVIDER", "auto")
    if provider not in {"auto", "asi", "ddgs"}:
        raise SearchUnavailable("SEARCH_PROVIDER must be auto, asi, or ddgs")
    notice = "Search results are leads; verify price, availability and product variant at the source."
    if provider in {"auto", "asi"}:
        try:
            sources = await asi_search(query)
            # Seek verbatim snippets for numerical claims even if ASI finds citations.
            if provider == "auto":
                snippets = await free_search(query)
                sources = snippets + [
                    s for s in sources if s.url not in {x.url for x in snippets}
                ]
                provider = "asi_web_search+ddgs"
            else:
                provider = "asi_web_search"
        except (TimeoutError, SearchUnavailable, aiohttp.ClientError, ValueError):
            if provider == "asi":
                raise SearchUnavailable(
                    "ASI search unavailable; choose auto or ddgs for fallback"
                )
            sources = await free_search(query)
            provider = "ddgs"
            notice += " ASI search unavailable; used the free search fallback."
    else:
        sources = await free_search(query)
    return sources, {
        "data_source": provider,
        "fetched_at": datetime.now(UTC).isoformat(),
        "sources": sources,
        "notice": notice,
    }
