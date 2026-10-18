"""Spotify on the laptop: play a song, artist, album or playlist, and control playback.

Only POST /spotify reaches this, and only with SPOTIFY_EXECUTOR_TOKEN, a private token
shared with the orchestrator (never with the browser, glasses or other agents). Unset or
empty refuses every request. The agent has no chat or agent-message protocol.

Drives the Spotify desktop app on this Mac through AppleScript (`osascript`, run without a
shell), so it works on a free account and needs no API keys. Spotify IDs come from keyless
web search restricted to open.spotify.com; only the ID is used, no result page is fetched.
macOS asks once to let the agent's terminal control Spotify (Privacy & Security >
Automation).
"""

import asyncio
import json
import os
import re
import secrets
import sys
from pathlib import Path

from pydantic.v1 import SecretStr

from .models import SpotifyRequest, SpotifyResponse
from .search import SearchUnavailable, search

KINDS = ("track", "artist", "album", "playlist")
LINK = re.compile(
    r"^https://open\.spotify\.com/(?:intl-[a-z-]+/)?(track|artist|album|playlist)/([A-Za-z0-9]{22})(?:[/?#]|$)"
)
DEVICE = "Spotify on this Mac"
# Seconds to wait for the app to start playing before reading what is playing.
SETTLE = 2.0

# --- Parsing chat text ---------------------------------------------------------------

CONTROLS = [
    ("now_playing", re.compile(
        r"^(?:now\s+playing|current\s+(?:song|track)|"
        r"what(?:'s|\s+is)\s+(?:playing|on)(?:\s+(?:now|right\s+now))?|"
        r"what\s+(?:song|track)\s+is\s+(?:this|playing|on)(?:\s+(?:now|right\s+now))?|"
        r"what\s+am\s+i\s+listening\s+to)$"
    )),
    ("pause", re.compile(r"^(?:pause|stop)(?:\s+(?:it|the\s+)?(?:music|song|spotify|playback))?$")),
    ("resume", re.compile(r"^(?:resume|unpause|continue|play)(?:\s+(?:it|the\s+)?(?:music|song|spotify|playback))?$")),
    ("next", re.compile(r"^(?:next|skip)(?:\s+(?:this\s+|the\s+)?(?:song|track|one))?$")),
    ("previous", re.compile(r"^(?:previous|prev|back|go\s+back)(?:\s+(?:song|track|one))?$|^last\s+(?:song|track)$")),
]
# Volume commands must be the whole message, so "play Kidz Bop volume 3" is a play and
# "turn down for what" is a song.
VOLUME = re.compile(
    r"(?:(?:set|change|put)\s+)?(?:the\s+)?volume\s+(?:to\s+|at\s+)?(\d{1,3})\s*%?"
    r"|(?:set\s+(?:the\s+)?volume\s+)?(\d{1,3})\s*%?\s+volume"
)
LOUDER = re.compile(
    r"(?:(?:a\s+)?(?:bit|little)\s+)?(?:turn\s+(?:it|this|the\s+(?:music|volume|song))\s+up"
    r"|turn\s+up\s+(?:the\s+)?(?:music|volume|song)|louder|volume\s+up|(?:raise|increase)\s+(?:the\s+)?volume)"
)
QUIETER = re.compile(
    r"(?:(?:a\s+)?(?:bit|little)\s+)?(?:turn\s+(?:it|this|the\s+(?:music|volume|song))\s+down"
    r"|turn\s+down\s+(?:the\s+)?(?:music|volume|song)|quieter|softer|volume\s+down|(?:lower|decrease)\s+(?:the\s+)?volume)"
)
VOLUME_STEP = 15
LEADING = re.compile(
    r"^(?:(?:hey|ok|okay|please|pls|can you|could you|would you)\b[\s,]*)*"
    r"(?:play|put on|start|listen to|queue up)\s+(?:me\s+)?(?:some\s+)?",
    re.IGNORECASE,
)
TRAILING = re.compile(r"(?:\s+(?:on|in|from)\s+spotify|\s+please|[\s?.!]+)$", re.IGNORECASE)
KIND_PREFIX = re.compile(r"^(?:the\s+|an?\s+)?(song|track|artist|band|album|playlist)\s+(?:called\s+|named\s+|by\s+)?", re.IGNORECASE)
KIND_SUFFIX = re.compile(r"\s+(song|track|artist|album|playlist)$", re.IGNORECASE)
KIND_ALIASES = {"song": "track", "band": "artist"}


def parse(text: str) -> SpotifyRequest:
    """'play the album Thriller' -> SpotifyRequest(action='play', kind='album', query='Thriller')."""
    clean = " ".join(text.lower().split()).strip(" ?.!")
    clean = re.sub(r"\s+(?:on|in)\s+spotify$|\s+please$|^(?:please|pls)\s+", "", clean)
    for action, pattern in CONTROLS:
        if pattern.fullmatch(clean):
            return SpotifyRequest(action=action, query=text[:300])
    if match := VOLUME.fullmatch(clean):
        return SpotifyRequest(action="volume", volume=min(int(match[1] or match[2]), 100), query=text[:300])
    if LOUDER.fullmatch(clean) or QUIETER.fullmatch(clean):
        step = VOLUME_STEP if LOUDER.fullmatch(clean) else -VOLUME_STEP
        return SpotifyRequest(action="volume", volume_change=step, query=text[:300])
    return SpotifyRequest(action="play", **play_target(text))


def play_target(text: str) -> dict:
    quoted = re.search(r'["“]([^"”]+)["”]', text)
    query = " ".join(text.split())
    query = TRAILING.sub("", LEADING.sub("", query, count=1))
    kind = "auto"
    for pattern in (KIND_PREFIX, KIND_SUFFIX):
        if match := pattern.search(query):
            kind = KIND_ALIASES.get(match[1].lower(), match[1].lower())
            query = (query[: match.start()] + query[match.end() :]).strip()
    if quoted and quoted[1].strip():
        query = quoted[1].strip()
    return {"query": (query or text)[:300], "kind": kind}


# --- The desktop app -----------------------------------------------------------------

class SpotifyError(Exception):
    """A failure with a message fit to show the user."""


# Arguments reach the script as argv, never spliced into its source.
SCRIPTS = {
    "running": 'return application "Spotify" is running',
    "play_uri": 'on run argv\ntell application "Spotify" to play track (item 1 of argv)\nend run',
    "pause": 'tell application "Spotify" to pause',
    "resume": 'tell application "Spotify" to play',
    "next": 'tell application "Spotify" to next track',
    "previous": 'tell application "Spotify" to previous track',
    "get_volume": 'tell application "Spotify" to return sound volume',
    "set_volume": 'on run argv\ntell application "Spotify" to set sound volume to (item 1 of argv as integer)\nend run',
    "now_playing": (
        'tell application "Spotify"\n'
        "if player state is stopped then return \"stopped\"\n"
        "set t to current track\n"
        "return (player state as string) & tab & (name of t) & tab & (artist of t) & tab & "
        "(spotify url of t) & tab & (album of t) & tab & ((player position as integer) as string) & tab & "
        "(((duration of t) div 1000) as string)\n"
        "end tell"
    ),
}


async def osascript(name: str, *args: str) -> str:
    if sys.platform != "darwin":
        raise SpotifyError("The Spotify agent only runs on a Mac with the Spotify app.")
    process = await asyncio.create_subprocess_exec(
        "osascript", "-e", SCRIPTS[name], *args,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    try:
        out, err = await asyncio.wait_for(process.communicate(), timeout=15)
    except TimeoutError:
        process.kill()
        raise SpotifyError("The Spotify app didn't respond.") from None
    if process.returncode != 0:
        message = err.decode(errors="replace")
        if "-1743" in message or "Not authorized" in message:
            raise SpotifyError(
                "macOS blocked control of Spotify: allow it in System Settings > "
                "Privacy & Security > Automation."
            )
        raise SpotifyError("The Spotify app refused the command.")
    return out.decode().strip()


async def require_running() -> None:
    if await osascript("running") != "true":
        raise SpotifyError("Spotify isn't open on the laptop. Open the Spotify app and try again.")


async def now_playing() -> dict | None:
    """What Spotify has loaded, or None when it is stopped (nothing loaded)."""
    parts = (await osascript("now_playing")).split("\t")
    if len(parts) != 7:
        return None
    state, title, artist, uri, album, position, duration = parts
    return {
        "state": state,
        "title": title,
        "artist": artist or None,
        "uri": uri,
        "album": album or None,
        "position": int(position) if position.isdigit() else None,
        "duration": int(duration) if duration.isdigit() and duration != "0" else None,
    }


def clock(seconds: int) -> str:
    return f"{seconds // 60}:{seconds % 60:02d}"


def is_named(playing: dict) -> bool:
    """A real track has a title; Spotify ads report an empty title or a dash."""
    return (playing.get("title") or "").strip(" -—–") != ""


def track_line(playing: dict) -> str:
    """'Espresso by Sabrina Carpenter'."""
    return playing["title"] + (f" by {playing['artist']}" if playing["artist"] else "")


def fill(reply: SpotifyResponse, playing: dict) -> None:
    reply.title, reply.artist, reply.album = playing["title"], playing["artist"], playing["album"]
    reply.state, reply.uri = playing["state"], playing["uri"]
    reply.position_seconds, reply.duration_seconds = playing["position"], playing["duration"]
    if reply.uri and reply.uri.count(":") == 2:
        kind, spotify_id = reply.uri.split(":")[1:]
        reply.url = f"https://open.spotify.com/{kind}/{spotify_id}"


# --- Finding Spotify IDs -------------------------------------------------------------

def result_title(title: str) -> str:
    """'Blinding Lights - song and lyrics by The Weeknd | Spotify' -> 'Blinding Lights'."""
    title = re.sub(r"\s*[|-]\s*Spotify\s*$", "", title or "")
    return re.split(r"\s+-\s+(?:song|single|album|ep|playlist)\b", title, maxsplit=1, flags=re.IGNORECASE)[0].strip()


def norm(text: str | None) -> str:
    return re.sub(r"[^a-z0-9]", "", (text or "").lower())


SPOTIFY_URI = re.compile(r"spotify:(?:track|artist|album|playlist):[A-Za-z0-9]{22}")
def found_file() -> Path:
    return Path(os.getenv("SPOTIFY_FOUND_FILE", ".runtime/spotify-found.json"))


def _found() -> dict[str, list[str]]:
    try:
        data = json.loads(found_file().read_text())
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}


def _remember(key: str, hit: tuple[str, str, str]) -> None:
    found = _found()
    found[key] = list(hit)
    while len(found) > 500:
        del found[next(iter(found))]
    try:
        found_file().parent.mkdir(parents=True, exist_ok=True)
        found_file().write_text(json.dumps(found))
    except OSError:
        pass


async def find(query: str, kind: str) -> tuple[str, str, str]:
    """(kind, uri, title) of the best open.spotify.com match, remembered once found (the
    web search behind it is often rate-limited). A failed search is retried once."""
    key = f"{kind}:{norm(query)}"
    known = _found().get(key)
    if isinstance(known, list) and len(known) == 3 and SPOTIFY_URI.fullmatch(str(known[1])):
        return known[0], known[1], known[2]
    try:
        hit = await _find(query, kind)
    except SearchUnavailable:
        await asyncio.sleep(0.5)
        hit = await _find(query, kind)
    _remember(key, hit)
    return hit


async def _find(query: str, kind: str) -> tuple[str, str, str]:
    """'auto' prefers an artist whose name is exactly the query, else the first track,
    else the first hit."""
    scope = "" if kind == "auto" else f"/{kind}"
    sources, _ = await search(f"site:open.spotify.com{scope} {query}")
    hits = []
    for source in sources:
        if match := LINK.match(source.url):
            if kind == "auto" or match[1] == kind:
                hits.append((match[1], f"spotify:{match[1]}:{match[2]}", result_title(source.title)))
    if not hits:
        raise LookupError
    if kind == "auto":
        for hit in hits:
            if hit[0] == "artist" and norm(hit[2]) == norm(query):
                return hit
        return next((hit for hit in hits if hit[0] == "track"), hits[0])
    return hits[0]


# --- Handler -------------------------------------------------------------------------

class SpotifyControlRequest(SpotifyRequest):
    """POST /spotify: a SpotifyRequest plus the orchestrator's executor token."""

    executor_token: SecretStr = SecretStr("")


def authorized(token: SecretStr) -> bool:
    expected = os.getenv("SPOTIFY_EXECUTOR_TOKEN", "")
    return bool(expected) and secrets.compare_digest(
        expected.encode(), token.get_secret_value().encode()
    )


async def control_authorized(req: SpotifyControlRequest) -> SpotifyResponse:
    """The REST entry point: refuses unless the executor token matches."""
    if not os.getenv("SPOTIFY_EXECUTOR_TOKEN", ""):
        message = "Spotify control on the laptop isn't set up (SPOTIFY_EXECUTOR_TOKEN is empty)."
        return SpotifyResponse(action=req.action, summary=message, error=message)
    if not authorized(req.executor_token):
        message = "Not authorized to control Spotify on the laptop."
        return SpotifyResponse(action=req.action, summary=message, error=message)
    return await control(SpotifyRequest(**req.dict(exclude={"executor_token"})))


async def control(req: SpotifyRequest) -> SpotifyResponse:
    reply = SpotifyResponse(action=req.action, query=req.query, device=DEVICE, summary="")
    try:
        if req.action == "play":
            await play(req, reply)
            return reply
        await require_running()
        if req.action == "now_playing":
            playing = await now_playing()
            if not playing:
                reply.state = "stopped"
                reply.summary = "Nothing is playing on Spotify."
                return reply
            fill(reply, playing)
            if playing["state"] == "playing":
                timing = (
                    f" ({clock(playing['position'])} / {clock(playing['duration'])})"
                    if playing["position"] is not None and playing["duration"]
                    else ""
                )
                reply.summary = f"▶️ {track_line(playing)}{timing}"
            else:
                reply.summary = f"⏸ Paused: {track_line(playing)}"
            return reply
        if req.action == "volume":
            level = req.volume
            if level is None:
                level = int(await osascript("get_volume")) + (req.volume_change or 0)
            level = max(0, min(100, level))
            await osascript("set_volume", str(level))
            reply.volume = level
            reply.summary = f"🔊 Volume set to {level}%."
            return reply
        await osascript(req.action)
        if req.action == "pause":
            reply.state = "paused"
            reply.summary = "⏸ Paused Spotify."
            return reply
        # Spotify accepts commands even on its login screen; check it actually played.
        await asyncio.sleep(SETTLE)
        playing = await now_playing()
        # Skipping while paused stays paused, so only resume must end up playing.
        if not playing or (req.action == "resume" and playing["state"] != "playing"):
            reply.state = playing["state"] if playing else "stopped"
            reply.error = "Spotify didn't start playing."
            reply.summary = "Spotify didn't start. Is the app logged in?"
            return reply
        fill(reply, playing)
        icon = {"resume": "▶️", "next": "⏭", "previous": "⏮"}[req.action]
        verb = "Now playing" if playing["state"] == "playing" else "Paused on"
        if is_named(playing):
            reply.summary = f"{icon} {verb} {track_line(playing)}"
        else:
            # Spotify Free plays ads between songs; an ad has no real title.
            done = {"resume": "Resumed", "next": "Skipped", "previous": "Went back"}[req.action]
            reply.summary = f"{icon} {done}. An ad or untitled item is playing."
    except SpotifyError as exc:
        reply.error = reply.summary = str(exc)
    except SearchUnavailable:
        reply.error = "Search unavailable."
        reply.summary = "Couldn't search for that right now; try again in a moment."
    return reply


async def play(req: SpotifyRequest, reply: SpotifyResponse) -> None:
    query = (req.query or "").strip()
    if not query:
        raise SpotifyError("Tell me what to play, e.g. “play Blinding Lights”.")
    try:
        kind, uri, title = await find(query, req.kind)
    except LookupError:
        reply.summary = f"Couldn't find “{query}” on Spotify."
        return
    # Telling Spotify to play opens the app if it is closed.
    await osascript("play_uri", uri)
    reply.kind, reply.uri, reply.title = kind, uri, title
    reply.url = f"https://open.spotify.com/{kind}/{uri.rsplit(':', 1)[1]}"
    await asyncio.sleep(SETTLE)
    playing = await now_playing()
    if not playing or playing["state"] != "playing":
        reply.state = playing["state"] if playing else "stopped"
        reply.error = "Spotify didn't start playing."
        reply.summary = f"Found {title} but Spotify didn't start. Is the app logged in?"
        return
    link = reply.url
    fill(reply, playing)
    # uri and url stay what was asked for (an album or playlist), not just its first track.
    reply.uri, reply.url = uri, link
    label = "" if kind == "track" else f" (from the {kind} {title})"
    reply.summary = f"▶️ Playing {track_line(playing)}{label} on the laptop."
