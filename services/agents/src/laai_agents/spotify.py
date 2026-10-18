"""Spotify on the laptop: play a song, artist, album or playlist, and control playback.

Uses the Spotify Web API with the Authorization Code + PKCE flow, so only a client ID is
needed (SPOTIFY_CLIENT_ID). Run `uv run laai-spotify-login` once on the laptop; it opens
the Spotify consent page and stores a refresh token in SPOTIFY_TOKEN_PATH (default
~/.config/laai/spotify_token.json, mode 600). The agent then targets the laptop's Spotify
app: the device named SPOTIFY_DEVICE_NAME, else the first "Computer" device, preferring
the active one. Web API playback control needs Spotify Premium.

Free accounts use the AppleScript backend instead: it drives the Spotify desktop app on
the Mac with osascript (fixed scripts, user values only as `on run argv` arguments) and
finds what to play with keyless web search on open.spotify.com. SPOTIFY_BACKEND picks it:
auto (default: AppleScript on macOS when no Web API login is stored), applescript or webapi.
"""

import asyncio
import base64
import hashlib
import json
import os
import re
import secrets
import sys
import time
import webbrowser
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlencode, urlsplit

import aiohttp

from . import net
from .models import SpotifyRequest, SpotifyResponse
from .search import SearchUnavailable, search

API = "https://api.spotify.com/v1"
AUTHORIZE_URL = "https://accounts.spotify.com/authorize"
TOKEN_URL = "https://accounts.spotify.com/api/token"
# Spotify only accepts loopback redirects as an explicit IP, not "localhost".
REDIRECT_URI = os.getenv("SPOTIFY_REDIRECT_URI", "http://127.0.0.1:8888/callback")
SCOPES = "user-read-playback-state user-modify-playback-state"
TIMEOUT = aiohttp.ClientTimeout(total=10)
KINDS = ("track", "artist", "album", "playlist")


class SpotifyError(Exception):
    """A failure with a message fit to show the user."""


def token_path() -> Path:
    default = Path.home() / ".config" / "laai" / "spotify_token.json"
    return Path(os.getenv("SPOTIFY_TOKEN_PATH", default)).expanduser()


def client_id() -> str:
    value = os.getenv("SPOTIFY_CLIENT_ID", "").strip()
    if not value:
        raise SpotifyError("Spotify isn't set up: set SPOTIFY_CLIENT_ID in .env.")
    return value


# --- Parsing chat text ---------------------------------------------------------------

CONTROLS = [
    ("pause", re.compile(r"^(?:pause|stop)(?:\s+(?:it|the\s+)?(?:music|song|spotify|playback))?$")),
    ("resume", re.compile(r"^(?:resume|unpause|continue|play)(?:\s+(?:it|the\s+)?(?:music|song|spotify|playback))?$")),
    ("next", re.compile(r"^(?:next|skip)(?:\s+(?:this\s+|the\s+)?(?:song|track|one))?$")),
    ("previous", re.compile(r"^(?:previous|prev|back|go\s+back)(?:\s+(?:song|track|one))?$|^last\s+(?:song|track)$")),
]
VOLUME = re.compile(r"\bvolume\b\D*(\d{1,3})\s*%?|\b(\d{1,3})\s*%?\s+volume\b")
LOUDER = re.compile(r"\b(?:turn\s+(?:it\s+)?up|louder|volume\s+up)\b")
QUIETER = re.compile(r"\b(?:turn\s+(?:it\s+)?down|quieter|softer|volume\s+down)\b")
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
        if pattern.match(clean):
            return SpotifyRequest(action=action, query=text[:300])
    if match := VOLUME.search(clean):
        return SpotifyRequest(action="volume", volume=min(int(match[1] or match[2]), 100), query=text[:300])
    if LOUDER.search(clean) or QUIETER.search(clean):
        step = VOLUME_STEP if LOUDER.search(clean) else -VOLUME_STEP
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


# --- Tokens --------------------------------------------------------------------------

_access: tuple[str, float] | None = None


def load_token() -> dict:
    try:
        return json.loads(token_path().read_text())
    except FileNotFoundError:
        raise SpotifyError("Spotify isn't linked yet: run `uv run laai-spotify-login` on the laptop.") from None


def save_token(data: dict) -> None:
    path = token_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.touch(mode=0o600, exist_ok=True)
    path.chmod(0o600)
    path.write_text(json.dumps(data))


async def access_token() -> str:
    global _access
    if _access and _access[1] > time.time() + 30:
        return _access[0]
    stored = load_token()
    form = {"grant_type": "refresh_token", "refresh_token": stored["refresh_token"], "client_id": client_id()}
    async with net.session().post(TOKEN_URL, data=form, timeout=TIMEOUT) as resp:
        body = await resp.json(content_type=None)
    if "access_token" not in body:
        raise SpotifyError("Spotify login expired: run `uv run laai-spotify-login` again.")
    # PKCE refresh tokens rotate; keep the new one.
    if body.get("refresh_token"):
        save_token({**stored, "refresh_token": body["refresh_token"]})
    _access = (body["access_token"], time.time() + body.get("expires_in", 3600))
    return _access[0]


# --- Web API -------------------------------------------------------------------------

async def call(method: str, path: str, params: dict | None = None, body: dict | None = None):
    """One Web API call. Returns parsed JSON, or None for an empty (204) reply."""
    global _access
    for attempt in range(2):
        headers = {"Authorization": f"Bearer {await access_token()}"}
        async with net.session().request(
            method, API + path, params=params, json=body, headers=headers, timeout=TIMEOUT
        ) as resp:
            if resp.status == 401 and attempt == 0:
                _access = None
                continue
            text = await resp.text()
            data = json.loads(text) if text.strip() else None
            if resp.status < 300:
                return data
            raise SpotifyError(api_error(resp.status, data))
    raise SpotifyError("Spotify rejected the login: run `uv run laai-spotify-login` again.")


def api_error(status: int, data) -> str:
    error = (data or {}).get("error") if isinstance(data, dict) else None
    reason = error.get("reason") if isinstance(error, dict) else None
    if reason == "PREMIUM_REQUIRED":
        return "Spotify only allows playback control with Premium."
    if reason == "NO_ACTIVE_DEVICE" or status == 404:
        return "Spotify isn't open on the laptop. Open the Spotify app and try again."
    if status == 429:
        return "Spotify is rate-limiting requests; try again in a moment."
    message = error.get("message") if isinstance(error, dict) else None
    return f"Spotify error {status}" + (f": {message}" if message else "")


async def laptop_device() -> dict:
    devices = (await call("GET", "/me/player/devices") or {}).get("devices", [])
    wanted = os.getenv("SPOTIFY_DEVICE_NAME", "").strip().lower()
    if wanted:
        candidates = [d for d in devices if d.get("name", "").lower() == wanted]
    else:
        candidates = [d for d in devices if d.get("type") == "Computer"]
    if not candidates:
        name = f'"{os.getenv("SPOTIFY_DEVICE_NAME")}"' if wanted else "on the laptop"
        raise SpotifyError(f"Spotify isn't open {name}. Open the Spotify app and try again.")
    return sorted(candidates, key=lambda d: not d.get("is_active"))[0]


async def find(query: str, kind: str) -> tuple[str, dict]:
    """Best match for the query. 'auto' prefers an exact artist name, else a track."""
    types = KINDS if kind == "auto" else (kind,)
    data = await call("GET", "/search", params={"q": query, "type": ",".join(types), "limit": 5})
    if kind == "auto":
        artists = [a for a in (data.get("artists") or {}).get("items", []) if a]
        if artists and artists[0]["name"].lower() == query.lower():
            return "artist", artists[0]
        kind = "track"
    items = [item for item in (data.get(f"{kind}s") or {}).get("items", []) if item]
    if not items:
        raise LookupError
    return kind, items[0]


def describe(kind: str, item: dict) -> tuple[str, str | None]:
    if kind in ("track", "album"):
        return item["name"], ", ".join(a["name"] for a in item.get("artists", [])) or None
    if kind == "playlist":
        return item["name"], (item.get("owner") or {}).get("display_name")
    return item["name"], None


# --- Handler -------------------------------------------------------------------------

def backend() -> str:
    """'applescript' or 'webapi'. Auto uses the Web API only once a login is stored."""
    choice = os.getenv("SPOTIFY_BACKEND", "auto").strip().lower()
    if choice in {"applescript", "webapi"}:
        return choice
    linked = token_path().exists() and bool(os.getenv("SPOTIFY_CLIENT_ID", "").strip())
    return "applescript" if sys.platform == "darwin" and not linked else "webapi"


async def control(req: SpotifyRequest) -> SpotifyResponse:
    if backend() == "applescript":
        return await desktop_control(req)
    return await webapi_control(req)


async def webapi_control(req: SpotifyRequest) -> SpotifyResponse:
    reply = SpotifyResponse(action=req.action, query=req.query, summary="")
    try:
        device = await laptop_device()
        reply.device = device.get("name")
        target = {"device_id": device["id"]}
        if req.action == "play":
            await play(req, target, reply)
        elif req.action == "pause":
            await call("PUT", "/me/player/pause", params=target)
            reply.summary = "⏸ Paused Spotify."
        elif req.action == "resume":
            await call("PUT", "/me/player/play", params=target)
            reply.summary = "▶️ Resumed Spotify."
        elif req.action == "next":
            await call("POST", "/me/player/next", params=target)
            reply.summary = "⏭ Skipped to the next track."
        elif req.action == "previous":
            await call("POST", "/me/player/previous", params=target)
            reply.summary = "⏮ Back to the previous track."
        elif req.action == "volume":
            current = device.get("volume_percent")
            if req.volume is not None:
                level = req.volume
            elif current is None:
                raise SpotifyError("This Spotify device doesn't report its volume.")
            else:
                level = current + (req.volume_change or 0)
            level = max(0, min(100, level))
            await call("PUT", "/me/player/volume", params={**target, "volume_percent": level})
            reply.volume = level
            reply.summary = f"🔊 Volume set to {level}%."
    except SpotifyError as exc:
        reply.error = str(exc)
        reply.summary = str(exc)
    except (aiohttp.ClientError, asyncio.TimeoutError):
        reply.error = "Couldn't reach Spotify."
        reply.summary = "Couldn't reach Spotify right now; try again in a moment."
    return reply


async def play(req: SpotifyRequest, target: dict, reply: SpotifyResponse) -> None:
    query = (req.query or "").strip()
    if not query:
        raise SpotifyError("Tell me what to play, e.g. “play Blinding Lights”.")
    try:
        kind, item = await find(query, req.kind)
    except LookupError:
        reply.summary = f"Couldn't find “{query}” on Spotify."
        return
    body = {"uris": [item["uri"]]} if kind == "track" else {"context_uri": item["uri"]}
    await call("PUT", "/me/player/play", params=target, body=body)
    reply.title, reply.artist = describe(kind, item)
    reply.kind, reply.uri = kind, item["uri"]
    reply.url = (item.get("external_urls") or {}).get("spotify")
    by = f" by {reply.artist}" if reply.artist and kind != "playlist" else ""
    label = "" if kind == "track" else f" ({kind})"
    reply.summary = f"▶️ Playing {reply.title}{by}{label} on {reply.device}."


# --- AppleScript backend (Spotify Free, desktop app on the Mac) ----------------------

# Fixed script sources. User values reach them only as `on run argv` arguments.
SCRIPTS = {
    "running": 'return application "Spotify" is running',
    "pause": 'tell application "Spotify" to pause',
    "resume": 'tell application "Spotify" to play',
    "next": 'tell application "Spotify" to next track',
    "previous": 'tell application "Spotify" to previous track',
    "get_volume": 'tell application "Spotify" to return sound volume',
    "set_volume": 'on run argv\ntell application "Spotify" to set sound volume to (item 1 of argv as integer)\nend run',
    "play_uri": 'on run argv\ntell application "Spotify" to play track (item 1 of argv)\nend run',
    "now_playing": (
        'tell application "Spotify" to return (name of current track) & linefeed & '
        "(artist of current track)"
    ),
}
URI = re.compile(r"spotify:(track|album|playlist|artist):[A-Za-z0-9]{22}")
WEB_LINK = re.compile(
    r"/(?:intl-[a-z]{2}(?:-[a-zA-Z]{2})?/)?(track|album|playlist|artist)/([A-Za-z0-9]{22})/?"
)
OSASCRIPT_TIMEOUT = 10
LAUNCH_WAIT = 10  # seconds for the app to start
SETTLE = 1.5  # seconds before reading what started playing
NOT_ALLOWED = ("-1743", "not allowed", "not authorized")
PERMISSION = (
    "macOS blocked controlling Spotify. Allow it in System Settings > Privacy & Security "
    "> Automation, then try again."
)


async def run_osascript(argv: list[str]) -> tuple[int, str, str]:
    process = await asyncio.create_subprocess_exec(
        *argv,
        stdin=asyncio.subprocess.DEVNULL,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    try:
        out, err = await asyncio.wait_for(process.communicate(), OSASCRIPT_TIMEOUT)
    except TimeoutError:
        process.kill()
        await process.wait()
        raise
    return process.returncode, out.decode(errors="replace"), err.decode(errors="replace")


def osascript_argv(script: str, *args: str) -> list[str]:
    return ["osascript", "-e", SCRIPTS[script], *args]


async def osascript(script: str, *args: str) -> str:
    try:
        code, out, err = await run_osascript(osascript_argv(script, *args))
    except (OSError, TimeoutError):
        raise SpotifyError("Couldn't control the Spotify app on the laptop.") from None
    if code != 0:
        if any(marker in err.lower() for marker in NOT_ALLOWED):
            raise SpotifyError(PERMISSION)
        raise SpotifyError("Couldn't control the Spotify app on the laptop.")
    return out.strip()


async def app_running() -> bool:
    return await osascript("running") == "true"


async def launch_app() -> None:
    try:
        code, _, _ = await run_osascript(["open", "-a", "Spotify"])
    except (OSError, TimeoutError):
        code = 1
    if code != 0:
        raise SpotifyError("Spotify isn't installed on the laptop. Install the Spotify app and try again.")
    for _ in range(LAUNCH_WAIT * 2):
        if await app_running():
            await asyncio.sleep(SETTLE)  # running before it accepts commands
            return
        await asyncio.sleep(0.5)
    raise SpotifyError("Spotify didn't start on the laptop. Open the Spotify app and try again.")


def spotify_uri(url: str) -> tuple[str, str] | None:
    """('track', 'spotify:track:<id>') for an open.spotify.com link, else None."""
    try:
        parts = urlsplit(url)
    except ValueError:
        return None
    if parts.scheme != "https" or parts.hostname != "open.spotify.com" or parts.username or parts.port:
        return None
    match = WEB_LINK.fullmatch(parts.path)
    if not match:
        return None
    uri = f"spotify:{match[1]}:{match[2]}"
    return (match[1], uri) if URI.fullmatch(uri) else None


def _clean_title(title: str) -> str:
    """'Daft Punk | Spotify' -> 'daft punk'."""
    return re.split(r"\s+[|\-–]\s+", title, maxsplit=1)[0].strip().lower()


async def find_uri(query: str, kind: str) -> tuple[str, str] | None:
    """Best open.spotify.com match from keyless web search. Search URLs are never fetched."""
    site = "site:open.spotify.com" + ("" if kind == "auto" else f"/{kind}")
    sources, _ = await search(f"{site} {query}")
    found = [(hit, s) for s in sources if (hit := spotify_uri(s.url))]
    if kind != "auto":
        found = [(hit, s) for hit, s in found if hit[0] == kind]
    elif artist := next(
        (hit for hit, s in found if hit[0] == "artist" and _clean_title(s.title) == query.lower()), None
    ):
        return artist
    else:
        # Like the Web API path: auto means a song unless the query is an artist's name.
        found.sort(key=lambda pair: pair[0][0] != "track")
    return found[0][0] if found else None


async def desktop_control(req: SpotifyRequest) -> SpotifyResponse:
    reply = SpotifyResponse(action=req.action, query=req.query, summary="", device="the laptop")
    try:
        running = await app_running()
        if req.action in {"play", "resume"} and not running:
            await launch_app()
        elif not running:
            raise SpotifyError("Spotify isn't open on the laptop. Open the Spotify app and try again.")
        if req.action == "play":
            await desktop_play(req, reply)
        elif req.action == "pause":
            await osascript("pause")
            reply.summary = "⏸ Paused Spotify."
        elif req.action == "resume":
            await osascript("resume")
            reply.summary = "▶️ Resumed Spotify."
        elif req.action == "next":
            await osascript("next")
            reply.summary = "⏭ Skipped to the next track."
        elif req.action == "previous":
            await osascript("previous")
            reply.summary = "⏮ Back to the previous track."
        elif req.action == "volume":
            if req.volume is not None:
                level = req.volume
            else:
                current = await osascript("get_volume")
                if not current.isdigit():
                    raise SpotifyError("The Spotify app didn't report its volume.")
                level = int(current) + (req.volume_change or 0)
            level = max(0, min(100, level))
            await osascript("set_volume", str(level))
            reply.volume = level
            reply.summary = f"🔊 Volume set to {level}%."
    except SpotifyError as exc:
        reply.error = str(exc)
        reply.summary = str(exc)
    return reply


async def desktop_play(req: SpotifyRequest, reply: SpotifyResponse) -> None:
    query = (req.query or "").strip()
    if not query:
        raise SpotifyError("Tell me what to play, e.g. “play Blinding Lights”.")
    try:
        hit = await find_uri(query, req.kind)
    except SearchUnavailable:
        raise SpotifyError("Couldn't search Spotify right now; try again in a moment.") from None
    if not hit:
        reply.summary = f"Couldn't find “{query}” on Spotify."
        return
    kind, uri = hit
    await osascript("play_uri", uri)
    await asyncio.sleep(SETTLE)
    try:
        playing = (await osascript("now_playing")).split("\n")
    except SpotifyError:
        playing = [""]  # No current track yet.
    reply.kind, reply.uri = kind, uri
    reply.url = f"https://open.spotify.com/{kind}/{uri.rsplit(':', 1)[1]}"
    reply.title = playing[0].strip()[:300] or None
    reply.artist = (playing[1].strip()[:300] if len(playing) > 1 else "") or None
    if not reply.title:
        reply.summary = f"Asked Spotify to play “{query}”, but it didn't report a track."
        return
    by = f" by {reply.artist}" if reply.artist else ""
    reply.summary = f"▶️ Playing {reply.title}{by} on the laptop's Spotify app."


# --- One-time login on the laptop ----------------------------------------------------

def login() -> None:
    """Open the Spotify consent page and store the refresh token. Run once per laptop."""
    from dotenv import load_dotenv

    load_dotenv()
    cid = client_id()
    verifier = secrets.token_urlsafe(64)
    challenge = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).rstrip(b"=").decode()
    state = secrets.token_urlsafe(16)
    url = AUTHORIZE_URL + "?" + urlencode({
        "client_id": cid,
        "response_type": "code",
        "redirect_uri": REDIRECT_URI,
        "code_challenge_method": "S256",
        "code_challenge": challenge,
        "scope": SCOPES,
        "state": state,
    })
    result: dict = {}

    class Callback(BaseHTTPRequestHandler):
        def do_GET(self):
            query = parse_qs(urlsplit(self.path).query)
            ok = query.get("state", [None])[0] == state and "code" in query
            if ok:
                result["code"] = query["code"][0]
            else:
                result["error"] = query.get("error", ["state mismatch"])[0]
            self.send_response(200)
            self.send_header("Content-Type", "text/plain; charset=utf-8")
            self.end_headers()
            self.wfile.write(b"Spotify linked. You can close this tab." if ok else b"Spotify login failed.")

        def log_message(self, *args):
            pass

    redirect = urlsplit(REDIRECT_URI)
    server = HTTPServer((redirect.hostname, redirect.port), Callback)
    print(f"Opening Spotify login. If no browser opens, visit:\n{url}")
    webbrowser.open(url)
    while not result:
        server.handle_request()
    server.server_close()
    if "code" not in result:
        raise SystemExit(f"Spotify login failed: {result['error']}")

    async def exchange() -> dict:
        form = {
            "grant_type": "authorization_code",
            "code": result["code"],
            "redirect_uri": REDIRECT_URI,
            "client_id": cid,
            "code_verifier": verifier,
        }
        async with aiohttp.ClientSession() as session:
            async with session.post(TOKEN_URL, data=form, timeout=TIMEOUT) as resp:
                return await resp.json(content_type=None)

    body = asyncio.run(exchange())
    if "refresh_token" not in body:
        raise SystemExit(f"Spotify login failed: {body.get('error_description') or body}")
    save_token({"refresh_token": body["refresh_token"], "scope": body.get("scope")})
    print(f"Spotify linked. Token saved to {token_path()}")
