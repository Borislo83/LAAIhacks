import json

import pytest

from laai_agents import spotify
from laai_agents.agents import build_spotify_agent
from laai_agents.models import SpotifyRequest

LAPTOP = {"id": "dev-laptop", "name": "Boris's MacBook", "type": "Computer", "is_active": False, "volume_percent": 50}
PHONE = {"id": "dev-phone", "name": "iPhone", "type": "Smartphone", "is_active": True, "volume_percent": 80}
TRACK = {
    "name": "Blinding Lights",
    "uri": "spotify:track:0VjIjW4GlUZAMYd2vXMi3b",
    "artists": [{"name": "The Weeknd"}],
    "external_urls": {"spotify": "https://open.spotify.com/track/0VjIjW4GlUZAMYd2vXMi3b"},
}
ARTIST = {"name": "Daft Punk", "uri": "spotify:artist:4tZwfgrHOc3mvqYlEYSvVi", "external_urls": {}}


@pytest.fixture
def api(monkeypatch):
    """Fake Web API: records every call, answers from `state`. No network."""
    calls = []
    state = {"devices": [PHONE, LAPTOP], "search": {"tracks": {"items": [TRACK]}, "artists": {"items": []}}}

    async def fake_call(method, path, params=None, body=None):
        calls.append((method, path, params, body))
        if "error" in state:
            raise spotify.SpotifyError(state["error"])
        if path == "/me/player/devices":
            return {"devices": state["devices"]}
        if path == "/search":
            return state["search"]
        return None

    monkeypatch.setattr(spotify, "call", fake_call)
    monkeypatch.delenv("SPOTIFY_DEVICE_NAME", raising=False)
    return calls, state


@pytest.mark.parametrize(
    "text, action, query, kind",
    [
        ("Play the album Thriller", "play", "Thriller", "album"),
        ('play "Hey Jude" on spotify', "play", "Hey Jude", "auto"),
        ("can you play some lofi playlist please", "play", "lofi", "playlist"),
        ("play the song Blinding Lights", "play", "Blinding Lights", "track"),
        ("Blinding Lights", "play", "Blinding Lights", "auto"),
        ("pause the music", "pause", None, None),
        ("play", "resume", None, None),
        ("skip this song", "next", None, None),
        ("go back", "previous", None, None),
    ],
)
def test_parse(text, action, query, kind):
    req = spotify.parse(text)
    assert req.action == action
    if query is not None:
        assert (req.query, req.kind) == (query, kind)


def test_parse_volume():
    assert spotify.parse("volume 40%").volume == 40
    assert spotify.parse("set volume to 150").volume == 100
    assert spotify.parse("turn it up").volume_change == 15
    assert spotify.parse("a bit quieter please").volume_change == -15


async def test_plays_track_on_laptop_not_active_phone(api):
    calls, _ = api
    reply = await spotify.control(SpotifyRequest(query="Blinding Lights"))
    assert reply.error is None
    assert (reply.title, reply.artist, reply.device) == ("Blinding Lights", "The Weeknd", "Boris's MacBook")
    assert reply.url == TRACK["external_urls"]["spotify"]
    assert calls[-1] == ("PUT", "/me/player/play", {"device_id": "dev-laptop"}, {"uris": [TRACK["uri"]]})
    assert "Playing Blinding Lights by The Weeknd" in reply.summary


async def test_auto_prefers_exact_artist(api):
    calls, state = api
    state["search"] = {"artists": {"items": [ARTIST]}, "tracks": {"items": [TRACK]}}
    reply = await spotify.control(SpotifyRequest(query="daft punk"))
    assert reply.kind == "artist"
    assert calls[-1][3] == {"context_uri": ARTIST["uri"]}


async def test_no_match(api):
    calls, state = api
    state["search"] = {"tracks": {"items": []}}
    reply = await spotify.control(SpotifyRequest(query="zzzz", kind="track"))
    assert reply.url is None and "Couldn't find" in reply.summary
    assert not any(path == "/me/player/play" for _, path, *_ in calls)


async def test_controls(api):
    calls, _ = api
    assert (await spotify.control(SpotifyRequest(action="pause"))).error is None
    assert calls[-1][:2] == ("PUT", "/me/player/pause")
    await spotify.control(SpotifyRequest(action="next"))
    assert calls[-1][:2] == ("POST", "/me/player/next")
    reply = await spotify.control(SpotifyRequest(action="volume", volume_change=60))
    assert reply.volume == 100
    assert calls[-1][2] == {"device_id": "dev-laptop", "volume_percent": 100}


async def test_named_device(api, monkeypatch):
    calls, _ = api
    monkeypatch.setenv("SPOTIFY_DEVICE_NAME", "iphone")
    await spotify.control(SpotifyRequest(action="pause"))
    assert calls[-1][2] == {"device_id": "dev-phone"}


async def test_spotify_closed_on_laptop(api):
    _, state = api
    state["devices"] = [PHONE]
    reply = await spotify.control(SpotifyRequest(query="Blinding Lights"))
    assert reply.error and "isn't open on the laptop" in reply.summary


async def test_api_errors_become_summaries(api):
    _, state = api
    state["error"] = spotify.api_error(403, {"error": {"status": 403, "reason": "PREMIUM_REQUIRED"}})
    reply = await spotify.control(SpotifyRequest(action="pause"))
    assert reply.error == "Spotify only allows playback control with Premium."


async def test_not_linked(monkeypatch, tmp_path):
    monkeypatch.setenv("SPOTIFY_TOKEN_PATH", str(tmp_path / "missing.json"))
    monkeypatch.setattr(spotify, "_access", None)
    reply = await spotify.control(SpotifyRequest(action="pause"))
    assert "laai-spotify-login" in reply.summary


def test_token_saved_private(monkeypatch, tmp_path):
    path = tmp_path / "laai" / "token.json"
    monkeypatch.setenv("SPOTIFY_TOKEN_PATH", str(path))
    spotify.save_token({"refresh_token": "r"})
    assert json.loads(path.read_text()) == {"refresh_token": "r"}
    assert path.stat().st_mode & 0o777 == 0o600


async def test_agent_builds():
    agent = build_spotify_agent()
    assert agent.name == "laai-spotify"
