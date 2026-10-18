"""YouTube link finder: a song or video the user asks for, as a canonical watch URL.

Providers, in order: the YouTube Data API (YOUTUBE_API_KEY), SerpApi's YouTube engine
(SERPAPI_API_KEY), keyless DDGS video search, then web search restricted to youtube.com.
Keyless "latest from <creator>" requests try the creator's channel RSS feed first. Only
single-video links are returned, rewritten to https://www.youtube.com/watch?v=<id>.
Shorts, playlists, channels and other sites are dropped. Result URLs are never fetched;
only fixed youtube.com oEmbed, channel and feed URLs are.
"""

import asyncio
import json
import os
import re
from datetime import UTC, datetime, timedelta
from difflib import SequenceMatcher
from urllib.parse import parse_qs, urlsplit

import aiohttp

from . import net
from .models import YouTubeRequest, YouTubeResponse, YouTubeVideo
from .progress import report, tracked
from .search import SearchUnavailable, search

VIDEO_ID = re.compile(r"[A-Za-z0-9_-]{11}")
TITLE_LIMIT = 120
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


def _video(url, title, channel=None, duration=None, published=None, views=None) -> dict | None:
    canonical = canonical_url(str(url or ""))
    if not canonical:
        return None
    try:
        views = int(views) if views not in (None, "") else None
    except (TypeError, ValueError):
        views = None
    return {
        "url": canonical,
        "title": " ".join(str(title or "").split())[:TITLE_LIMIT] or "YouTube video",
        "channel": " ".join(str(channel).split())[:100] if channel else None,
        "duration": str(duration) if duration else None,
        "published": str(published)[:32] if published else None,
        "views": views,
    }


async def _api_get(path: str, params: dict) -> dict:
    key = os.getenv("YOUTUBE_API_KEY", "")
    if not key:
        raise SearchUnavailable("YouTube Data API key is not configured")
    async with net.session().get(
        f"https://www.googleapis.com/youtube/v3/{path}",
        params={**params, "key": key},
        timeout=aiohttp.ClientTimeout(total=10),
    ) as response:
        if response.status != 200:
            # Never echo the response: it can include the request URL and its key.
            raise SearchUnavailable(f"YouTube Data API returned HTTP {response.status}")
        return await response.json()


def _api_videos(data: dict) -> list[dict]:
    return [
        video
        for item in data.get("items", [])
        if (
            video := _video(
                "https://www.youtube.com/watch?v=" + str(item.get("id", {}).get("videoId", "")),
                item.get("snippet", {}).get("title"),
                item.get("snippet", {}).get("channelTitle"),
                published=item.get("snippet", {}).get("publishedAt"),
            )
        )
    ]


async def api_search(query: str, want: dict | None = None) -> list[dict]:
    if want and want["recent"] and want["creator"]:
        # "Latest": find the creator's channel, then its uploads newest first.
        channels = await _api_get(
            "search", {"part": "snippet", "type": "channel", "maxResults": "5", "q": want["creator"]}
        )
        channel_id = next(
            (
                item.get("id", {}).get("channelId")
                for item in channels.get("items", [])
                if same_creator(item.get("snippet", {}).get("title"), want["creator"])
                and item.get("id", {}).get("channelId")
            ),
            None,
        )
        if channel_id:
            uploads = await _api_get(
                "search",
                {"part": "snippet", "type": "video", "channelId": channel_id,
                 "order": "date", "maxResults": "6"},
            )
            return _api_videos(uploads)
    data = await _api_get(
        "search", {"part": "snippet", "type": "video", "maxResults": "10", "q": query}
    )
    return _api_videos(data)


AGO = re.compile(r"(\d+)\s+(second|minute|hour|day|week|month|year)s?\s+ago", re.IGNORECASE)
AGO_SECONDS = {"second": 1, "minute": 60, "hour": 3600, "day": 86400, "week": 604800,
               "month": 2629800, "year": 31557600}


def _published_from_ago(text: str | None) -> str | None:
    """'2 weeks ago' -> an ISO timestamp, so newest-first sorting works."""
    match = AGO.search(text or "")
    if not match:
        return None
    age = int(match[1]) * AGO_SECONDS[match[2].lower()]
    return (datetime.now(UTC) - timedelta(seconds=age)).isoformat()


async def serpapi_search(query: str, want: dict | None = None) -> list[dict]:
    key = os.getenv("SERPAPI_API_KEY", "")
    if not key:
        raise SearchUnavailable("SerpApi key is not configured")
    params = {"engine": "youtube", "search_query": query, "api_key": key}
    if want and want["recent"]:
        params["sp"] = "CAI="  # YouTube's "sort by upload date"
    async with net.session().get(
        "https://serpapi.com/search.json", params=params, timeout=aiohttp.ClientTimeout(total=15)
    ) as response:
        if response.status != 200:
            raise SearchUnavailable(f"SerpApi returned HTTP {response.status}")
        data = await response.json()
    videos = []
    for item in data.get("video_results", [])[:20]:
        views = item.get("views")
        video = _video(
            item.get("link"),
            item.get("title"),
            (item.get("channel") or {}).get("name"),
            item.get("length"),
            _published_from_ago(item.get("published_date")),
            views if isinstance(views, int) else None,
        )
        if video:
            videos.append(video)
    return videos


async def ddgs_search(query: str, timelimit: str | None = None) -> list[dict]:
    def run():
        from ddgs import DDGS

        return DDGS(timeout=8).videos(
            query, region="us-en", safesearch="moderate", timelimit=timelimit, max_results=20
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
                row.get("published"),
                (row.get("statistics") or {}).get("viewCount"),
            )
        )
    ]


def result_title(raw: str) -> str:
    """One video's title from a web result title.

    Search providers sometimes merge several results into one title, e.g.
    "How to tie a tie - YouTubeHow to Tie a Tie in 10 Seconds ...HOW TO TIE - YouTube".
    The text before the first "- YouTube" (or "...") is this result's own title.
    """
    title = " ".join(raw.split())
    title = re.split(r"\s*[-–|]\s*YouTube(?!rs?\b)", title, maxsplit=1, flags=re.IGNORECASE)[0]
    title = re.split(r"\s*(?:\.\.\.|…)", title, maxsplit=1)[0]
    return title.strip(" -–|")[:TITLE_LIMIT]


# Channel names that web results state explicitly; anything less certain stays None.
CHANNEL = re.compile(
    r"(?:\bYouTube\s*·\s*|\buploaded by\s+)([^·|\n]{2,80}?)\s*(?:·|\||$|\.\s)",
    re.IGNORECASE,
)


def result_channel(snippet: str) -> str | None:
    match = CHANNEL.search(" ".join(snippet.split()))
    return match[1].strip() if match else None


async def web_search(query: str) -> list[dict]:
    sources, _ = await search(f"site:youtube.com {query}")
    return [
        video
        for s in sources
        if (video := _video(s.url, result_title(s.title), result_channel(s.snippet)))
    ]


RECENT = re.compile(
    r"\b(?:latest|newest|new|recent|most\s+recent|last|today'?s|this\s+week'?s)\b", re.IGNORECASE
)
# Re-uploads and variations that are not the original, unless the user asked for them.
NOT_ORIGINAL = re.compile(
    r"\b(?:cover|lyrics?|lyric\s+video|slowed|sped\s*up|reverb|nightcore|8d|karaoke|reaction|"
    r"instrumental|1\s*hour|loop|fan\s*made|tribute|piano\s+version|remix|mashup|edit)\b",
    re.IGNORECASE,
)


def norm(text: str | None) -> str:
    return re.sub(r"[^a-z0-9]", "", (text or "").lower())


NOT_AN_ARTIST = re.compile(
    r"(?:me|you|us|him|her|them|it|my\s+side|your\s+side|night|day|now|myself|"
    r"yourself|design|default|nature|chance|proxy|accident|heart)",
    re.IGNORECASE,
)


def same_creator(channel: str | None, creator: str | None) -> bool:
    """'Markiplier' matches 'markipiliers'; 'BoyWithUke - Topic' and 'BoyWithUkeVEVO' match."""
    if not channel or not creator:
        return False
    chan = norm(re.sub(r"\s*-\s*topic$|vevo$|\bofficial\b", "", channel, flags=re.IGNORECASE))
    if not chan:
        return False  # "Official" or "VEVO" alone names no one; empty would match anything
    for wanted in {norm(creator), norm(creator).removesuffix("s")}:
        if len(wanted) >= 3 and (wanted in chan or chan in wanted):
            return True
        if wanted and SequenceMatcher(None, chan, wanted).ratio() >= 0.82:
            return True
    return False


def intent(query: str, kind: str) -> dict:
    """What the request is after: a creator/artist, a title, and whether newest matters."""
    recent = bool(RECENT.search(query))
    core = " ".join(RECENT.sub(" ", query).split())
    creator, title = None, core
    # The last " by " separates title and artist ("Stand by Me by Ben E. King"), and a
    # pronoun or filler word is part of the title, not an artist ("Stand by Me").
    by = re.match(r"^(.+)\s+by\s+(.+)$", core, re.IGNORECASE)
    if by and NOT_AN_ARTIST.fullmatch(by[2].strip()):
        by = None
    dash = re.match(r"^(.+?)\s+[-–]\s+(.+)$", core)
    owner = re.match(r"^(.+?)(?:'s|s'|’s)\s+(.+)$", core, re.IGNORECASE)
    if by:
        title, creator = by[1], by[2]
    elif dash:
        creator, title = dash[1], dash[2]
    elif owner:
        creator, title = owner[1], owner[2]
    elif recent:
        # "markipliers latest": everything left names the creator.
        creator, title = core, ""
    if creator:
        creator = re.sub(r"(?:'s|’s|s')$", "", creator.strip(), flags=re.IGNORECASE)
    return {"creator": creator, "title": title.strip(), "recent": recent, "kind": kind}


def rank(videos: list[dict], want: dict, query: str) -> list[dict]:
    asked_variant = bool(NOT_ORIGINAL.search(query))
    title_words = set(re.findall(r"[a-z0-9]+", want["title"].lower()))

    def score(v):
        points = 0.0
        if same_creator(v["channel"], want["creator"]):
            points += 4
            if v["channel"] and re.search(r"-\s*topic$|vevo$", v["channel"], re.IGNORECASE):
                points += 0.5  # the label's own upload
        if title_words:
            words = set(re.findall(r"[a-z0-9]+", v["title"].lower()))
            points += 2 * len(title_words & words) / len(title_words)
        if not asked_variant and NOT_ORIGINAL.search(v["title"]):
            points -= 3
        if v["views"]:
            points += min(len(str(v["views"])), 10) / 10  # popularity, gently
        return points

    ranked = sorted(videos, key=score, reverse=True)
    if want["recent"]:
        # Newest upload from that creator's own channel; others only if none match.
        own = [v for v in ranked if same_creator(v["channel"], want["creator"])]
        dated = [v for v in own if v["published"]]
        if dated:
            newest = sorted(dated, key=lambda v: v["published"], reverse=True)
            return newest + [v for v in ranked if v not in newest]
    return ranked


# --- Keyless channel data -------------------------------------------------------------
# Without an API key, YouTube's public oEmbed endpoint names a video's channel, and a
# channel's public RSS feed lists its newest uploads with dates. Only these fixed
# youtube.com URLs are requested; channel links are validated before use.
YT = "https://www.youtube.com"
CHANNEL_URL = re.compile(
    r"^https://(?:www\.|m\.)?youtube\.com/(?:channel/(?P<id>UC[\w-]{22})|user/(?P<user>[\w.-]{1,100})"
    r"|(?P<handle>@[\w.-]{1,100})|c/(?P<custom>[\w.-]{1,100}))"
    r"(?:/(?:videos|featured|streams|about))?/?$"
)
CHANNEL_ID_IN_PAGE = re.compile(
    r'"(?:externalId|channelId)":"(UC[\w-]{22})"|youtube\.com/channel/(UC[\w-]{22})'
)
PAGE_LIMIT = 3_000_000
_oembed: dict[str, tuple[str | None, str | None]] = {}


async def _fetch_text(url: str, params: dict | None = None) -> str | None:
    try:
        async with net.session().get(
            url,
            params=params,
            timeout=aiohttp.ClientTimeout(total=8),
            headers={"User-Agent": "Mozilla/5.0", "Accept-Language": "en-US,en;q=0.9"},
            allow_redirects=False,
        ) as response:
            if response.status != 200:
                return None
            # read(n) returns only what has arrived so far; collect the whole body.
            chunks, size = [], 0
            async for chunk in response.content.iter_chunked(65536):
                size += len(chunk)
                if size > PAGE_LIMIT:
                    return None
                chunks.append(chunk)
            return b"".join(chunks).decode("utf-8", errors="replace")
    except (aiohttp.ClientError, TimeoutError, ValueError):
        return None


async def oembed_channel(url: str) -> tuple[str | None, str | None]:
    """(channel name, channel URL) for one watch URL, from YouTube's oEmbed endpoint."""
    if url in _oembed:
        return _oembed[url]
    text = await _fetch_text(f"{YT}/oembed", {"url": url, "format": "json"})
    name = channel_url = None
    if text:
        try:
            data = json.loads(text)
            name = " ".join(str(data.get("author_name") or "").split())[:100] or None
            link = str(data.get("author_url") or "")
            channel_url = link if CHANNEL_URL.match(link) else None
        except ValueError:
            pass
    if len(_oembed) > 512:
        _oembed.clear()
    _oembed[url] = (name, channel_url)
    return name, channel_url


async def with_channels(videos: list[dict], limit: int = 8) -> list[dict]:
    """Fill in the uploading channel for results that lack it (web search results)."""
    missing = [v for v in videos[:limit] if not v["channel"]]
    found = await asyncio.gather(*(oembed_channel(v["url"]) for v in missing))
    for video, (name, channel_url) in zip(missing, found):
        video["channel"] = name
        video["channel_url"] = channel_url
    return videos


async def feed_url_for(channel_url: str) -> str | None:
    match = CHANNEL_URL.match(channel_url)
    if not match:
        return None
    if match["id"]:
        return f"{YT}/feeds/videos.xml?channel_id={match['id']}"
    if match["user"]:
        return f"{YT}/feeds/videos.xml?user={match['user']}"
    # @handle and /c/ pages carry the channel ID in their HTML.
    page_path = match["handle"] or f"c/{match['custom']}"
    page = await _fetch_text(f"{YT}/{page_path}")
    found = CHANNEL_ID_IN_PAGE.search(page or "")
    channel_id = found and (found[1] or found[2])
    return f"{YT}/feeds/videos.xml?channel_id={channel_id}" if channel_id else None


ATOM = {"a": "http://www.w3.org/2005/Atom", "yt": "http://www.youtube.com/xml/schemas/2015",
        "media": "http://search.yahoo.com/mrss/"}


def parse_feed(xml: str) -> list[dict]:
    """A channel feed's uploads, newest first as YouTube lists them."""
    from xml.etree import ElementTree

    try:
        root = ElementTree.fromstring(xml)
    except ElementTree.ParseError:
        return []
    videos = []
    for entry in root.findall("a:entry", ATOM):
        stats = entry.find("media:group/media:community/media:statistics", ATOM)
        video = _video(
            f"{YT}/watch?v={entry.findtext('yt:videoId', '', ATOM)}",
            entry.findtext("a:title", "", ATOM),
            entry.findtext("a:author/a:name", None, ATOM),
            published=entry.findtext("a:published", None, ATOM),
            views=stats.get("views") if stats is not None else None,
        )
        if video:
            videos.append(video)
    return videos


async def latest_from_feed(creator: str) -> list[dict]:
    """The creator's newest uploads from their channel's RSS feed, or nothing."""
    sources, _ = await search(f"{creator} youtube channel")
    # Results mix the real channel with Topic channels and look-alikes; score them.
    mentions: dict[str, int] = {}
    for source in sources:
        channel_id = re.search(r"/channel/(UC[\w-]{22})", source.url)
        if channel_id:
            mentions[channel_id[1]] = mentions.get(channel_id[1], 0) + 1
    scored = []
    for source in sources:
        match = CHANNEL_URL.match(source.url)
        name = result_title(source.title)
        if not match or not same_creator(name, creator):
            continue
        score = 3 if norm(name) in {norm(creator), norm(creator).removesuffix("s")} else 1
        if re.search(r"-\s*topic$", name, re.IGNORECASE):
            score -= 2  # auto-generated music channel, not the creator's uploads
        if match["handle"] and re.search(r"\d{3,}$", match["handle"]):
            score -= 2  # "@markiplier2278"-style look-alike handles
        if match["id"]:
            score += mentions.get(match["id"], 0) - 1
        scored.append((score, source.url))
    channel_urls = [url for score, url in sorted(scored, reverse=True) if score > 0]
    if not channel_urls:
        # No channel page in the results: ask who uploaded the creator's videos.
        videos = [v for s in sources if (v := _video(s.url, result_title(s.title)))]
        for video in (await with_channels(videos, limit=6)):
            if video.get("channel_url") and same_creator(video["channel"], creator):
                channel_urls.append(video["channel_url"])
    for channel_url in list(dict.fromkeys(channel_urls))[:2]:
        feed = await feed_url_for(channel_url)
        xml = await _fetch_text(feed) if feed else None
        uploads = parse_feed(xml) if xml else []
        # The feed's own channel name must be the creator, not a Topic or fan channel.
        owner = uploads[0]["channel"] if uploads else None
        if owner and same_creator(owner, creator) and not re.search(r"-\s*topic$", owner, re.IGNORECASE):
            return uploads
    return []


PROVIDERS = (
    ("youtube_data_api", api_search),
    ("serpapi_youtube", serpapi_search),
    ("ddgs_videos", ddgs_search),
    ("web_search", web_search),
)
# Providers that report the channel and upload date, which accuracy depends on.
NEEDS_KEY = {"youtube_data_api": "YOUTUBE_API_KEY", "serpapi_youtube": "SERPAPI_API_KEY"}


@tracked("youtube")
async def find_video(req: YouTubeRequest) -> YouTubeResponse:
    kind = resolve_kind(req)
    title_query = parse_query(req.query)
    want = intent(title_query, kind)
    query = title_query
    if want["recent"] and want["creator"]:
        query = want["creator"]  # dates decide "latest"; the word itself skews results
    elif kind == "song" and "official" not in query.lower():
        query = f"{want['creator'] or ''} {want['title']} official audio".strip()
    report("searching", f"Searching YouTube for {title_query}")
    videos, provider, answered = [], None, False
    lookup_failed = False  # "latest" needs the channel lookup; a search outage isn't "not found"
    if want["recent"] and want["creator"] and not os.getenv("YOUTUBE_API_KEY"):
        try:
            videos = await latest_from_feed(want["creator"])
        except (SearchUnavailable, aiohttp.ClientError, TimeoutError, ValueError):
            videos, lookup_failed = [], True
        if videos:
            provider, answered = "youtube_rss", True
    for name, run in PROVIDERS if not videos else ():
        if name in NEEDS_KEY and not os.getenv(NEEDS_KEY[name]):
            continue
        try:
            found = []
            if name == "ddgs_videos" and want["recent"]:
                # Recent uploads first; widen only if the creator has none this month.
                found = await run(query, "m")
                if not any(same_creator(v["channel"], want["creator"]) for v in found):
                    found += await run(query)
            elif name in NEEDS_KEY:
                found = await run(query, want)
            else:
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
        if lookup_failed:
            return YouTubeResponse(
                query=title_query,
                kind=kind,
                data_source="unavailable",
                error="YouTube search unavailable",
                summary=f"Couldn't look up {want['creator']}'s channel right now; search is "
                "temporarily unavailable. Try again in a moment.",
            )
        return YouTubeResponse(
            query=title_query,
            kind=kind,
            summary=f"No YouTube video found for {title_query}.",
        )
    if provider in {"web_search", "ddgs_videos"}:
        if kind == "song" and want["creator"] and want["title"]:
            # A second phrasing finds the artist's own upload more often.
            try:
                more = await web_search(f"{want['creator']} - {want['title']}")
            except SearchUnavailable:
                more = []
            unique += [v for v in more if v["url"] not in seen and not seen.add(v["url"])]
        report("checking_channels", "Checking which channel uploaded each result")
        unique = await with_channels(unique)
    unique = rank(unique, want, title_query)
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
