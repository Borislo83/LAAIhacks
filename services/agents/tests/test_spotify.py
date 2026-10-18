import pytest

from laai_agents import spotify
from laai_agents.agents import build_spotify_agent
from laai_agents.models import Source, SpotifyRequest

TRACK_ID = "6qYkmqFsXbj8CQjAdbYz07"
ARTIST_ID = "4tZwfgrHOc3mvqYlEYSvVi"


def playing_line(state, title="Blinding Lights", artist="The Weeknd", position="102", duration="200"):
    return "\t".join([state, title, artist, f"spotify:track:{TRACK_ID}", "After Hours", position, duration])


def source(url, title):
    return Source(title=title, url=url)


@pytest.fixture
def app(monkeypatch):
    """Fake Spotify app and search: records osascript calls and search queries. No processes, no network."""
    calls, queries = [], []
    state = {
        "running": "true",
        "volume": "50",
        "now_playing": playing_line("playing"),
        "sources": [
            source(f"https://open.spotify.com/track/{TRACK_ID}", "Blinding Lights - song and lyrics by The Weeknd | Spotify"),
        ],
    }

    async def fake_osascript(name, *args):
        calls.append((name, *args))
        if "error" in state:
            raise spotify.SpotifyError(state["error"])
        return {"running": state["running"], "get_volume": state["volume"], "now_playing": state["now_playing"]}.get(name, "")

    async def fake_search(query):
        queries.append(query)
        return state["sources"], {}

    monkeypatch.setattr(spotify, "osascript", fake_osascript)
    monkeypatch.setattr(spotify, "search", fake_search)
    monkeypatch.setattr(spotify, "SETTLE", 0)
    return calls, queries, state


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
        ("what's playing", "now_playing", None, None),
        ("What song is this?", "now_playing", None, None),
        ("now playing", "now_playing", None, None),
        ("what is playing right now", "now_playing", None, None),
    ],
)
def test_parse(text, action, query, kind):
    req = spotify.parse(text)
    assert req.action == action
    if query is not None:
        assert (req.query, req.kind) == (query, kind)


@pytest.mark.parametrize(
    "text, query",
    [
        ("play Kidz Bop volume 3", "Kidz Bop volume 3"),
        ("turn down for what", "turn down for what"),
        ("play turn down for what", "turn down for what"),
        ("louder than bombs", "louder than bombs"),
    ],
)
def test_volume_words_inside_a_title_are_a_play(text, query):
    req = spotify.parse(text)
    assert (req.action, req.query, req.volume, req.volume_change) == ("play", query, None, None)


def test_parse_volume():
    assert spotify.parse("volume 40%").volume == 40
    assert spotify.parse("turn the volume down").volume_change == -15
    assert spotify.parse("set volume to 150").volume == 100
    assert spotify.parse("turn it up").volume_change == 15
    assert spotify.parse("a bit quieter please").volume_change == -15


@pytest.mark.parametrize(
    "title, expected",
    [
        ("Blinding Lights - song and lyrics by The Weeknd | Spotify", "Blinding Lights"),
        ("Thriller - Album by Michael Jackson | Spotify", "Thriller"),
        ("Daft Punk | Spotify", "Daft Punk"),
        ("Lofi Beats - playlist by Spotify | Spotify", "Lofi Beats"),
    ],
)
def test_result_title(title, expected):
    assert spotify.result_title(title) == expected


async def test_plays_track_and_reads_back(app):
    calls, queries, _ = app
    reply = await spotify.control(SpotifyRequest(query="Blinding Lights", kind="track"))
    assert reply.error is None
    assert queries == ["site:open.spotify.com/track Blinding Lights"]
    assert ("play_uri", f"spotify:track:{TRACK_ID}") in calls
    assert (reply.title, reply.artist) == ("Blinding Lights", "The Weeknd")
    assert reply.url == f"https://open.spotify.com/track/{TRACK_ID}"
    assert reply.summary == "▶️ Playing Blinding Lights by The Weeknd on the laptop."


async def test_auto_prefers_exact_artist(app):
    calls, _, state = app
    state["sources"] = [
        source(f"https://open.spotify.com/track/{TRACK_ID}", "One More Time - song and lyrics by Daft Punk | Spotify"),
        source(f"https://open.spotify.com/intl-de/artist/{ARTIST_ID}?si=abc", "Daft Punk | Spotify"),
    ]
    reply = await spotify.control(SpotifyRequest(query="daft punk"))
    assert reply.kind == "artist"
    assert ("play_uri", f"spotify:artist:{ARTIST_ID}") in calls


async def test_ignores_non_spotify_and_wrong_kind_links(app):
    calls, _, state = app
    state["sources"] = [
        source("https://open.spotify.com.evil.example/track/" + TRACK_ID, "x"),
        source(f"https://open.spotify.com/album/{TRACK_ID}", "x"),
        source("https://open.spotify.com/track/short", "x"),
    ]
    reply = await spotify.control(SpotifyRequest(query="zzzz", kind="track"))
    assert "Couldn't find" in reply.summary
    assert not any(name == "play_uri" for name, *_ in calls)


async def test_reports_when_spotify_does_not_start(app):
    _, _, state = app
    state["now_playing"] = "stopped"
    reply = await spotify.control(SpotifyRequest(query="Blinding Lights"))
    assert reply.error == "Spotify didn't start playing."
    assert "logged in" in reply.summary


@pytest.mark.parametrize("action", ["resume", "next", "previous"])
async def test_controls_report_when_spotify_stays_stopped(app, action):
    # The app accepts commands on its login screen but never plays.
    _, _, state = app
    state["now_playing"] = "stopped"
    reply = await spotify.control(SpotifyRequest(action=action))
    assert reply.error == "Spotify didn't start playing."
    assert reply.summary == "Spotify didn't start. Is the app logged in?"


async def test_skip_while_paused_is_fine(app):
    _, _, state = app
    state["now_playing"] = playing_line("paused")
    assert (await spotify.control(SpotifyRequest(action="next"))).error is None
    assert (await spotify.control(SpotifyRequest(action="resume"))).error == "Spotify didn't start playing."


async def test_controls(app):
    calls, _, _ = app
    assert (await spotify.control(SpotifyRequest(action="pause"))).summary == "⏸ Paused Spotify."
    assert calls[-1] == ("pause",)
    await spotify.control(SpotifyRequest(action="next"))
    assert ("next",) in calls
    reply = await spotify.control(SpotifyRequest(action="resume"))
    assert reply.summary == "▶️ Now playing Blinding Lights by The Weeknd"
    reply = await spotify.control(SpotifyRequest(action="volume", volume_change=60))
    assert reply.volume == 100 and calls[-1] == ("set_volume", "100")


async def test_controls_need_running_app(app):
    calls, _, state = app
    state["running"] = "false"
    reply = await spotify.control(SpotifyRequest(action="pause"))
    assert "isn't open" in reply.error
    assert ("pause",) not in calls


async def test_osascript_errors_become_summaries(app):
    _, _, state = app
    state["error"] = "macOS blocked control of Spotify: allow it in System Settings."
    reply = await spotify.control(SpotifyRequest(action="pause"))
    assert reply.summary.startswith("macOS blocked control of Spotify")


def test_scripts_take_args_as_argv():
    # User text must never be spliced into AppleScript source.
    assert "item 1 of argv" in spotify.SCRIPTS["play_uri"]
    assert "item 1 of argv" in spotify.SCRIPTS["set_volume"]


async def test_agent_builds_rest_only(monkeypatch):
    monkeypatch.delenv("AGENTVERSE_MAILBOX", raising=False)
    agent = build_spotify_agent()
    assert agent.name == "laai-spotify"
    # No typed or chat protocol: only the token-gated REST route reaches Spotify.
    assert agent.protocols == {}


async def test_public_seed_is_refused_on_agentverse(monkeypatch):
    monkeypatch.setenv("AGENTVERSE_MAILBOX", "1")
    monkeypatch.delenv("SPOTIFY_AGENT_SEED", raising=False)
    assert build_spotify_agent() is None
    monkeypatch.setenv("SPOTIFY_AGENT_SEED", "a private test seed for spotify")
    assert build_spotify_agent() is not None


TOKEN = "spotify-test-token"


@pytest.mark.parametrize("configured, sent", [("", ""), ("", "x"), (TOKEN, ""), (TOKEN, "wrong")])
async def test_rest_refuses_without_the_executor_token(app, monkeypatch, configured, sent):
    calls, queries, _ = app
    monkeypatch.setenv("SPOTIFY_EXECUTOR_TOKEN", configured)
    reply = await spotify.control_authorized(
        spotify.SpotifyControlRequest(action="pause", executor_token=sent)
    )
    assert reply.error and calls == [] and queries == []


async def test_rest_runs_with_the_executor_token(app, monkeypatch):
    calls, _, _ = app
    monkeypatch.setenv("SPOTIFY_EXECUTOR_TOKEN", TOKEN)
    reply = await spotify.control_authorized(
        spotify.SpotifyControlRequest(action="pause", executor_token=TOKEN)
    )
    assert reply.error is None and ("pause",) in calls


async def test_now_playing_while_playing(app):
    calls, _, _ = app
    reply = await spotify.control(SpotifyRequest(action="now_playing"))
    assert reply.summary == "▶️ Blinding Lights by The Weeknd (1:42 / 3:20)"
    assert (reply.state, reply.album, reply.position_seconds, reply.duration_seconds) == ("playing", "After Hours", 102, 200)
    assert reply.url == f"https://open.spotify.com/track/{TRACK_ID}"
    assert calls[-1] == ("now_playing",)


async def test_now_playing_paused_and_stopped(app):
    _, _, state = app
    state["now_playing"] = playing_line("paused", "Espresso", "Sabrina Carpenter")
    reply = await spotify.control(SpotifyRequest(action="now_playing"))
    assert reply.summary == "⏸ Paused: Espresso by Sabrina Carpenter"
    state["now_playing"] = "stopped"
    reply = await spotify.control(SpotifyRequest(action="now_playing"))
    assert (reply.summary, reply.state, reply.error) == ("Nothing is playing on Spotify.", "stopped", None)


async def test_now_playing_without_timing(app):
    _, _, state = app
    state["now_playing"] = playing_line("playing", duration="0")
    reply = await spotify.control(SpotifyRequest(action="now_playing"))
    assert reply.summary == "▶️ Blinding Lights by The Weeknd"


@pytest.mark.parametrize("action, summary", [("next", "⏭ Now playing Espresso by Sabrina Carpenter"), ("previous", "⏮ Now playing Espresso by Sabrina Carpenter")])
async def test_skips_name_the_track(app, action, summary):
    _, _, state = app
    state["now_playing"] = playing_line("playing", "Espresso", "Sabrina Carpenter")
    assert (await spotify.control(SpotifyRequest(action=action))).summary == summary


async def test_album_play_keeps_album_link(app):
    _, _, state = app
    state["sources"] = [source(f"https://open.spotify.com/album/{ARTIST_ID}", "After Hours - Album by The Weeknd | Spotify")]
    reply = await spotify.control(SpotifyRequest(query="After Hours", kind="album"))
    assert reply.uri == f"spotify:album:{ARTIST_ID}" and reply.url.endswith(f"/album/{ARTIST_ID}")
    assert reply.summary == "▶️ Playing Blinding Lights by The Weeknd (from the album After Hours) on the laptop."
