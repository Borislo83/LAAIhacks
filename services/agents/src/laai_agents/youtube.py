"""YouTube link finder: a song or video the user asks for, as a canonical watch URL.

Providers, in order: the YouTube Data API (only when YOUTUBE_API_KEY is set), keyless
DDGS video search, then web search restricted to youtube.com. Only single-video links
are returned, rewritten to https://www.youtube.com/watch?v=<id>. Shorts, playlists,
channels and other sites are dropped. No URL from the query or results is fetched.
"""

import asyncio
import os
import re
from urllib.parse import parse_qs, urlsplit

import aiohttp

from . import net
from .models import YouTubeRequest, YouTubeResponse, YouTubeVideo
from .progress import report, tracked
from .search import SearchUnavailable, search

VIDEO_ID = re.compile(r"[A-Za-z0-9_-]{11}")
WATCH_HOSTS = {"youtube.com", "www.youtube.com", "m.youtube.com", "music.youtube.com"}
SONG_WORDS = re.compile(r"\b(?:song|track|music|audio|lyrics|single)\b", re.IGNORECASE)

# Request phrasing around the actual title, stripped repeatedly from either end.
KIND_WORDS = r"(?:youtube|yt|music\s+video|video|song|track|audio|clip|link|url)s?"
LEADING = [
    re.compile(p, re.IGNORECASE)
    for p in (
        r"^(?:hey|ok|okay|please|pls|can you|could you|would you|i want to|i'd like to)\b[\s,]*",
        r"^(?:find|search(?:\s+for)?|look\s+up|play|get|show|give|send|pull\s+up|open)\b\s*",
        r"^me\b\s*",
        # Articles and connectors only around request words, so "The Scientist" survives.
        rf"^(?:the|a|an)\s+(?={KIND_WORDS}\b)",
        rf"^{KIND_WORDS}\b(?:\s+(?:named|name|called|titled|for|to|of)\b)?[\s:]*",
    )
]
TRAILING = [
    re.compile(p, re.IGNORECASE)
    for p in (
        r"[\s?.!]+$",
        r"\s+(?:on|from|in)\s+(?:youtube|yt)$",
        r"\s+(?:youtube|yt)(?:\s+(?:link|video|url))?$",
        r"\s+(?:link|url|video)$",
        r"\s+please$",
    )
]


def parse_query(text: str) -> str:
    """'find the song "Blinding Lights"' -> 'Blinding Lights'."""
    quoted = re.search(r'["“]([^"”]+)["”]', text)
    if quoted and quoted[1].strip():
        return " ".join(quoted[1].split())
    query = " ".join(text.split())
    changed = True
    while changed:
        before = query
        for pattern in LEADING + TRAILING:
            query = pattern.sub("", query)
        query = query.strip(" \"'“”‘’:,-")
        changed = query != before
    # A request that is all phrasing ("play a song") keeps its words rather than nothing.
    return query or " ".join(text.split())


def resolve_kind(req: YouTubeRequest) -> str:
    if req.kind != "auto":
        return req.kind
    if re.search(r"\bmusic\s+video\b", req.query, re.IGNORECASE):
        return "video"
    return "song" if SONG_WORDS.search(req.query) else "video"


def canonical_url(url: str) -> str | None:
    """The watch URL for a single YouTube video link, or None for anything else."""
    try:
        parsed = urlsplit(url.strip())
        if parsed.scheme != "https" or parsed.username or parsed.port not in {None, 443}:
            return None
        host = (parsed.hostname or "").lower()
        if host in WATCH_HOSTS and parsed.path == "/watch":
            ids = parse_qs(parsed.query).get("v", [])
            video = ids[0] if len(ids) == 1 else ""
        elif host == "youtu.be":
            video = parsed.path.removeprefix("/")
        else:
            return None
    except ValueError:
        return None
    if not VIDEO_ID.fullmatch(video):
        return None
    return f"https://www.youtube.com/watch?v={video}"


def _video(url, title, channel=None, duration=None) -> dict | None:
    canonical = canonical_url(str(url or ""))
    if not canonical:
        return None
    return {
        "url": canonical,
        "title": " ".join(str(title or "").split())[:200] or "YouTube video",
        "channel": " ".join(str(channel).split())[:100] if channel else None,
        "duration": str(duration) if duration else None,
    }


async def api_search(query: str) -> list[dict]:
    key = os.getenv("YOUTUBE_API_KEY", "")
    if not key:
        raise SearchUnavailable("YouTube Data API key is not configured")
    async with net.session().get(
        "https://www.googleapis.com/youtube/v3/search",
        params={
            "part": "snippet",
            "type": "video",
            "maxResults": "6",
            "q": query,
            "key": key,
        },
        timeout=aiohttp.ClientTimeout(total=10),
    ) as response:
        if response.status != 200:
            # Never echo the response: it can include the request URL and its key.
            raise SearchUnavailable(f"YouTube Data API returned HTTP {response.status}")
        data = await response.json()
    return [
        video
        for item in data.get("items", [])
        if (
            video := _video(
                "https://www.youtube.com/watch?v=" + str(item.get("id", {}).get("videoId", "")),
                item.get("snippet", {}).get("title"),
                item.get("snippet", {}).get("channelTitle"),
            )
        )
    ]


async def ddgs_search(query: str) -> list[dict]:
    def run():
        from ddgs import DDGS

        return DDGS(timeout=8).videos(
            query, region="us-en", safesearch="moderate", max_results=12
        )

    try:
        rows = await asyncio.wait_for(asyncio.to_thread(run), timeout=20)
    except Exception as exc:
        raise SearchUnavailable("Free video search unavailable") from exc
    return [
        video
        for row in rows or []
        if (
            video := _video(
                row.get("content") or row.get("embed_url"),
                row.get("title"),
                row.get("uploader") or row.get("publisher"),
                row.get("duration"),
            )
        )
    ]


async def web_search(query: str) -> list[dict]:
    sources, _ = await search(f"site:youtube.com {query}")
    return [
        video
        for s in sources
        if (video := _video(s.url, s.title.removesuffix(" - YouTube")))
    ]


PROVIDERS = (
    ("youtube_data_api", api_search),
    ("ddgs_videos", ddgs_search),
    ("web_search", web_search),
)


@tracked("youtube")
async def find_video(req: YouTubeRequest) -> YouTubeResponse:
    kind = resolve_kind(req)
    title_query = parse_query(req.query)
    query = title_query
    if kind == "song" and "official" not in query.lower():
        query += " official audio"
    report("searching", f"Searching YouTube for {title_query}")
    videos, provider, answered = [], None, False
    for name, run in PROVIDERS:
        if name == "youtube_data_api" and not os.getenv("YOUTUBE_API_KEY"):
            continue
        try:
            found = await run(query)
        except (SearchUnavailable, aiohttp.ClientError, TimeoutError, ValueError):
            continue
        answered = True
        if found:
            videos, provider = found, name
            break
    if not answered:
        return YouTubeResponse(
            query=title_query,
            kind=kind,
            data_source="unavailable",
            error="YouTube search unavailable",
            summary="YouTube search is unavailable right now; no link was guessed.",
        )
    unique, seen = [], set()
    for video in videos:
        if video["url"] not in seen:
            seen.add(video["url"])
            unique.append(video)
    if not unique:
        return YouTubeResponse(
            query=title_query,
            kind=kind,
            summary=f"No YouTube video found for {title_query}.",
        )
    best, *rest = unique
    report("found", f"Found {best['title']}")
    lines = [best["title"]]
    if best["channel"]:
        lines.append(f"Channel: {best['channel']}")
    lines.append(best["url"])
    return YouTubeResponse(
        query=title_query,
        kind=kind,
        title=best["title"],
        url=best["url"],
        channel=best["channel"],
        duration=best["duration"],
        alternatives=[
            YouTubeVideo(title=v["title"], url=v["url"], channel=v["channel"])
            for v in rest[:3]
        ],
        summary="\n".join(lines),
        data_source=provider,
    )
