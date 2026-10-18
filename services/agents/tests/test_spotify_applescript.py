"""Spotify Free: the AppleScript backend drives the desktop app. No real osascript runs."""

import pytest

from laai_agents import spotify
from laai_agents.models import Source, SpotifyRequest

TRACK_ID = "0VjIjW4GlUZAMYd2vXMi3b"
ARTIST_ID = "4tZwfgrHOc3mvqYlEYSvVi"
# Captured at import, before conftest swaps it for a blocker.
REAL_RUN = spotify.run_osascript


@pytest.fixture
def app(monkeypatch):
    """Fake osascript/open: records argv, answers from `state`."""
    runs = []
    state = {"running": True, "volume": "50", "now": "Blinding Lights\nThe Weeknd", "fail": None, "results": []}

    async def fake_run(argv):
        runs.append(argv)
        if state["fail"]:
            return 1, "", state["fail"]
        if argv[0] == "open":
            state["running"] = True
            return 0, "", ""
        script = argv[2]
        if script == spotify.SCRIPTS["running"]:
            return 0, "true\n" if state["running"] else "false\n", ""
        if script == spotify.SCRIPTS["get_volume"]:
            return 0, state["volume"] + "\n", ""
        if script == spotify.SCRIPTS["now_playing"]:
            return 0, state["now"] + "\n", ""
        return 0, "", ""

    async def fake_search(query):
        state["query"] = query
        return state["results"], {}

    async def no_sleep(seconds):
        pass

    monkeypatch.setenv("SPOTIFY_BACKEND", "applescript")
    monkeypatch.setattr(spotify, "run_osascript", fake_run)
    monkeypatch.setattr(spotify, "search", fake_search)
    monkeypatch.setattr(spotify.asyncio, "sleep", no_sleep)
    return runs, state


def scripts(runs):
    """The script name of each osascript run, plus its argv arguments."""
    names = {source: name for name, source in spotify.SCRIPTS.items()}
    return [
        ("open", *argv[1:]) if argv[0] == "open" else (names[argv[2]], *argv[3:])
        for argv in runs
    ]


@pytest.mark.parametrize(
    "action,script,summary",
    [
        ("pause", "pause", "Paused"),
        ("resume", "resume", "Resumed"),
        ("next", "next", "next track"),
        ("previous", "previous", "previous track"),
    ],
)
async def test_controls_map_to_fixed_scripts(app, action, script, summary):
    runs, _ = app
    reply = await spotify.control(SpotifyRequest(action=action))
    assert reply.error is None and summary in reply.summary
    assert scripts(runs) == [("running",), (script,)]
    assert all(argv[:2] == ["osascript", "-e"] for argv in runs)


@pytest.mark.parametrize(
    "req,level",
    [
        (SpotifyRequest(action="volume", volume=40), 40),
        (SpotifyRequest(action="volume", volume_change=15), 65),
        (SpotifyRequest(action="volume", volume_change=90), 100),
        (SpotifyRequest(action="volume", volume_change=-90), 0),
    ],
)
async def test_volume_is_clamped_and_passed_as_an_argument(app, req, level):
    runs, _ = app
    reply = await spotify.control(req)
    assert reply.volume == level and f"{level}%" in reply.summary
    assert scripts(runs)[-1] == ("set_volume", str(level))
    assert str(level) not in runs[-1][2]  # the value is never in the script source


async def test_volume_change_reads_current_volume(app):
    runs, state = app
    state["volume"] = "missing value"
    reply = await spotify.control(SpotifyRequest(action="volume", volume_change=15))
    assert reply.error and "volume" in reply.error
    assert "set_volume" not in [s[0] for s in scripts(runs)]


async def test_plays_a_track_found_by_web_search(app):
    runs, state = app
    state["results"] = [
        Source(title="Blinding Lights - song by The Weeknd | Spotify", url=f"https://open.spotify.com/track/{TRACK_ID}?si=abc")
    ]
    reply = await spotify.control(SpotifyRequest(query="Blinding Lights", kind="track"))
    assert state["query"] == "site:open.spotify.com/track Blinding Lights"
    assert ("play_uri", f"spotify:track:{TRACK_ID}") in scripts(runs)
    assert (reply.title, reply.artist, reply.kind) == ("Blinding Lights", "The Weeknd", "track")
    assert reply.uri == f"spotify:track:{TRACK_ID}" and reply.url == f"https://open.spotify.com/track/{TRACK_ID}"
    assert reply.summary == "▶️ Playing Blinding Lights by The Weeknd on the laptop's Spotify app."


async def test_auto_prefers_an_exact_artist_then_a_track(app):
    runs, state = app
    state["results"] = [
        Source(title="Get Lucky | Spotify", url="https://open.spotify.com/album/" + "A" * 22),
        Source(title="Daft Punk | Spotify", url=f"https://open.spotify.com/intl-de/artist/{ARTIST_ID}"),
    ]
    reply = await spotify.control(SpotifyRequest(query="daft punk"))
    assert reply.kind == "artist" and ("play_uri", f"spotify:artist:{ARTIST_ID}") in scripts(runs)
    runs.clear()
    state["results"].append(Source(title="Around the World", url=f"https://open.spotify.com/track/{TRACK_ID}"))
    reply = await spotify.control(SpotifyRequest(query="around the world"))
    assert reply.kind == "track"


async def test_nothing_found_is_said_honestly(app):
    runs, state = app
    state["results"] = [Source(title="Spotify", url="https://evil.example/track/" + TRACK_ID)]
    reply = await spotify.control(SpotifyRequest(query="zzzz"))
    assert "Couldn't find" in reply.summary and reply.uri is None
    assert "play_uri" not in [s[0] for s in scripts(runs)]


async def test_search_outage_is_an_error(app, monkeypatch):
    async def down(query):
        raise spotify.SearchUnavailable("down")

    monkeypatch.setattr(spotify, "search", down)
    reply = await spotify.control(SpotifyRequest(query="Blinding Lights"))
    assert reply.error and "search" in reply.error


@pytest.mark.parametrize(
    "url",
    [
        f"http://open.spotify.com/track/{TRACK_ID}",
        f"https://open.spotify.com.evil.example/track/{TRACK_ID}",
        f"https://user@open.spotify.com/track/{TRACK_ID}",
        f"https://open.spotify.com:8443/track/{TRACK_ID}",
        f"https://open.spotify.com/episode/{TRACK_ID}",
        "https://open.spotify.com/track/short",
        f"https://open.spotify.com/track/{TRACK_ID}x",
        'https://open.spotify.com/track/0VjIjW4GlUZAMYd2vX"&quit',
        "https://open.spotify.com/track/0VjIjW4GlUZAMYd2vXMi3b/../../x",
        "https://open.spotify.com/track/%22%20%26%20do%20shell%20script",
        "spotify:track:" + TRACK_ID,
        "",
    ],
)
def test_only_strict_spotify_links_become_uris(url):
    assert spotify.spotify_uri(url) is None


def test_uri_conversion():
    assert spotify.spotify_uri(f"https://open.spotify.com/playlist/{TRACK_ID}?si=1") == (
        "playlist",
        f"spotify:playlist:{TRACK_ID}",
    )


async def test_play_opens_the_app_when_it_is_closed(app):
    runs, state = app
    state["running"] = False
    state["results"] = [Source(title="x", url=f"https://open.spotify.com/track/{TRACK_ID}")]
    reply = await spotify.control(SpotifyRequest(query="Blinding Lights", kind="track"))
    assert ("open", "-a", "Spotify") in scripts(runs) and reply.error is None


async def test_controls_do_not_open_a_closed_app(app):
    runs, state = app
    state["running"] = False
    reply = await spotify.control(SpotifyRequest(action="next"))
    assert "isn't open" in reply.error and scripts(runs) == [("running",)]


async def test_automation_permission_denied(app):
    _, state = app
    state["fail"] = "execution error: Not authorized to send Apple events to Spotify. (-1743)"
    reply = await spotify.control(SpotifyRequest(action="pause"))
    assert "Privacy & Security" in reply.error and reply.summary == reply.error


async def test_osascript_uses_an_argv_list_not_a_shell(monkeypatch):
    seen = {}

    class Process:
        returncode = 0

        async def communicate(self):
            return b"true\n", b""

    async def fake_exec(*argv, **kwargs):
        seen["argv"], seen["kwargs"] = argv, kwargs
        return Process()

    monkeypatch.setattr(spotify.asyncio, "create_subprocess_exec", fake_exec)
    argv = spotify.osascript_argv("play_uri", 'spotify:track:x" & (do shell script "id") & "')
    assert await REAL_RUN(argv) == (0, "true\n", "")
    assert seen["argv"] == ("osascript", "-e", spotify.SCRIPTS["play_uri"], argv[-1])
    assert "shell" not in seen["kwargs"]


@pytest.mark.parametrize(
    "env,linked,platform,expected",
    [
        ("applescript", True, "darwin", "applescript"),
        ("webapi", False, "darwin", "webapi"),
        ("auto", False, "darwin", "applescript"),
        ("auto", True, "darwin", "webapi"),
        ("auto", False, "linux", "webapi"),
        ("", False, "darwin", "applescript"),
    ],
)
def test_backend_selection(monkeypatch, tmp_path, env, linked, platform, expected):
    token = tmp_path / "token.json"
    if linked:
        token.write_text("{}")
    monkeypatch.setenv("SPOTIFY_TOKEN_PATH", str(token))
    monkeypatch.setenv("SPOTIFY_CLIENT_ID", "cid")
    monkeypatch.setenv("SPOTIFY_BACKEND", env or "auto")
    monkeypatch.setattr(spotify.sys, "platform", platform)
    assert spotify.backend() == expected
