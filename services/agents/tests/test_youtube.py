import pytest

from laai_agents import youtube
from laai_agents.agents import build_youtube_agent
from laai_agents.models import Source, YouTubeRequest, YouTubeResponse

ID = "fJ9rUzIMcZQ"
_real_web_search = youtube.web_search
_real_ddgs_search = youtube.ddgs_search
WATCH = f"https://www.youtube.com/watch?v={ID}"


@pytest.fixture
def providers(monkeypatch):
    """Fake providers: no network. Records the queries each one received."""
    monkeypatch.delenv("YOUTUBE_API_KEY", raising=False)
    calls = {"ddgs": [], "web": []}
    results = {"ddgs": [], "web": []}

    def fake(name):
        async def run(query):
            calls[name].append(query)
            value = results[name]
            if isinstance(value, Exception):
                raise value
            return value

        return run

    monkeypatch.setattr(youtube, "ddgs_search", fake("ddgs"))
    monkeypatch.setattr(youtube, "web_search", fake("web"))
    monkeypatch.setattr(
        youtube,
        "PROVIDERS",
        (
            ("youtube_data_api", youtube.api_search),
            ("ddgs_videos", youtube.ddgs_search),
            ("web_search", youtube.web_search),
        ),
    )
    return calls, results


@pytest.mark.parametrize(
    "text, expected",
    [
        ('find the song "Bohemian Rhapsody"', "Bohemian Rhapsody"),
        ("find the song name Blinding Lights by The Weeknd", "Blinding Lights by The Weeknd"),
        ("give me a youtube link for how to tie a tie", "how to tie a tie"),
        ("Can you send me the YouTube video link for lofi hip hop radio?", "lofi hip hop radio"),
        ("play The Scientist on youtube", "The Scientist"),
        ("youtube link to To Be Loved", "To Be Loved"),
    ],
)
def test_request_phrasing_is_stripped(text, expected):
    assert youtube.parse_query(text) == expected


@pytest.mark.parametrize(
    "url, expected",
    [
        (WATCH, WATCH),
        (f"https://youtube.com/watch?v={ID}&t=42s&list=PL123", WATCH),
        (f"https://m.youtube.com/watch?v={ID}", WATCH),
        (f"https://youtu.be/{ID}?si=tracking", WATCH),
        (f"http://www.youtube.com/watch?v={ID}", None),
        (f"https://www.youtube.com/shorts/{ID}", None),
        ("https://www.youtube.com/playlist?list=PL1234567890", None),
        ("https://www.youtube.com/@QueenOfficial", None),
        (f"https://www.youtube.com.evil.example/watch?v={ID}", None),
        (f"https://user@www.youtube.com/watch?v={ID}", None),
        (f"https://vimeo.com/watch?v={ID}", None),
        ("https://www.youtube.com/watch?v=short", None),
        (f"https://www.youtube.com/watch?v={ID}&v=dQw4w9WgXcQ", None),
        ("javascript:alert(1)", None),
    ],
)
def test_only_single_video_links_are_returned_in_canonical_form(url, expected):
    assert youtube.canonical_url(url) == expected


async def test_song_query_biases_toward_official_audio(providers):
    calls, results = providers
    results["ddgs"] = [
        youtube._video(f"https://youtu.be/{ID}", "Queen - Bohemian Rhapsody", "Queen Official", "6:00"),
        youtube._video(WATCH, "duplicate", "x"),
        youtube._video("https://www.youtube.com/watch?v=dQw4w9WgXcQ", "Live", "Queen"),
    ]
    result = await youtube.find_video(YouTubeRequest(query='find the song "Bohemian Rhapsody"'))
    assert calls["ddgs"] == ["Bohemian Rhapsody official audio"] and not calls["web"]
    assert (result.kind, result.url, result.channel, result.duration) == (
        "song",
        WATCH,
        "Queen Official",
        "6:00",
    )
    assert [a.url for a in result.alternatives] == ["https://www.youtube.com/watch?v=dQw4w9WgXcQ"]
    assert "Queen Official" in result.summary and WATCH in result.summary


async def test_video_query_is_searched_as_is(providers):
    calls, results = providers
    results["ddgs"] = [youtube._video(WATCH, "How to Tie a Tie", "Tie Guy")]
    result = await youtube.find_video(
        YouTubeRequest(query="give me a youtube video link for how to tie a tie")
    )
    assert calls["ddgs"] == ["how to tie a tie"]
    assert result.kind == "video" and result.title == "How to Tie a Tie"


async def test_falls_back_to_site_search_when_video_search_fails(providers, monkeypatch):
    calls, results = providers
    results["ddgs"] = youtube.SearchUnavailable("down")

    async def search(query):
        calls["web"].append(query)
        return [
            Source(title="Not YouTube", url="https://example.com/v"),
            Source(title="Lofi radio - YouTube", url=f"https://youtu.be/{ID}"),
        ], {}

    monkeypatch.setattr(youtube, "search", search)
    monkeypatch.setattr(
        youtube,
        "PROVIDERS",
        (("ddgs_videos", youtube.ddgs_search), ("web_search", _real_web_search)),
    )
    result = await youtube.find_video(YouTubeRequest(query="lofi radio", kind="video"))
    assert calls["web"] == ["site:youtube.com lofi radio"]
    assert result.url == WATCH and result.title == "Lofi radio"
    assert result.data_source == "web_search"


async def test_no_results_returns_no_link(providers):
    result = await youtube.find_video(YouTubeRequest(query="zzzz no such video"))
    assert result.url is None and result.error is None
    assert "No YouTube video found" in result.summary


async def test_search_failure_sets_error_without_guessing(providers):
    _, results = providers
    results["ddgs"] = results["web"] = youtube.SearchUnavailable("down")
    result = await youtube.find_video(YouTubeRequest(query="anything", kind="song"))
    assert result.url is None and result.error == "YouTube search unavailable"
    assert result.data_source == "unavailable"


async def test_api_key_uses_data_api_first(providers, monkeypatch):
    calls, _ = providers
    monkeypatch.setenv("YOUTUBE_API_KEY", "test-key")

    async def api(query):
        return [youtube._video(WATCH, "From API", "Channel")]

    monkeypatch.setattr(
        youtube,
        "PROVIDERS",
        (("youtube_data_api", api), *youtube.PROVIDERS[1:]),
    )
    result = await youtube.find_video(YouTubeRequest(query="video", kind="video"))
    assert result.data_source == "youtube_data_api" and not calls["ddgs"]


async def test_rest_route_and_chat_request(providers):
    _, results = providers
    results["ddgs"] = [youtube._video(WATCH, "Song", "Artist")]
    agent = build_youtube_agent()
    handler = agent._rest_handlers[("POST", "/youtube")]
    response = await handler(None, YouTubeRequest(query="play the song Song"))
    assert isinstance(response, YouTubeResponse) and response.url == WATCH
    assert response.summary.splitlines() == ["Song", "Channel: Artist", WATCH]


def test_request_validation():
    with pytest.raises(ValueError):
        YouTubeRequest(query="")
    with pytest.raises(ValueError):
        YouTubeRequest(query="x" * 301)
    with pytest.raises(ValueError):
        YouTubeRequest(query="x", kind="playlist")


async def test_ddgs_rows_keep_only_youtube_videos(monkeypatch):
    import ddgs

    class FakeDDGS:
        def __init__(self, timeout):
            pass

        def videos(self, query, **kwargs):
            return [
                {"content": "https://vimeo.com/123", "title": "Other site"},
                {"content": f"https://www.youtube.com/shorts/{ID}", "title": "Short"},
                {
                    "content": f"https://www.youtube.com/watch?v={ID}",
                    "title": "  Real   video ",
                    "uploader": "Channel",
                    "publisher": "YouTube",
                    "duration": "3:21",
                },
            ]

    monkeypatch.setattr(ddgs, "DDGS", FakeDDGS)
    assert await _real_ddgs_search("q") == [
        {"url": WATCH, "title": "Real video", "channel": "Channel", "duration": "3:21"}
    ]


MERGED_TITLE = (
    "How to tie a tie - Quick and Easy - YouTubeHow to Tie a Tie in 10 Seconds: "
    "The Easiest Method for ...HOW TO TIE A TIE - YouTube..."
)


async def test_web_results_with_merged_titles_use_their_own_title(providers, monkeypatch):
    """Regression: a live web result's title concatenated several YouTube results."""

    async def search(query):
        return [
            Source(
                title=MERGED_TITLE,
                url="https://www.youtube.com/watch?v=9BMhFmNzw-o",
                snippet="YouTube · Tie Guy · 12M views · 3 years ago",
            ),
            Source(title="Tie in 10 seconds - YouTube", url=f"https://youtu.be/{ID}"),
        ], {}

    _, results = providers
    results["ddgs"] = youtube.SearchUnavailable("down")
    monkeypatch.setattr(youtube, "search", search)
    monkeypatch.setattr(
        youtube,
        "PROVIDERS",
        (("ddgs_videos", youtube.ddgs_search), ("web_search", _real_web_search)),
    )
    result = await youtube.find_video(
        YouTubeRequest(query="youtube link for how to tie a tie", kind="auto")
    )
    assert result.url == "https://www.youtube.com/watch?v=9BMhFmNzw-o"
    assert result.title == "How to tie a tie - Quick and Easy"
    assert result.channel == "Tie Guy"
    assert [(a.title, a.channel) for a in result.alternatives] == [("Tie in 10 seconds", None)]


@pytest.mark.parametrize(
    "raw, title",
    [
        ("The Weeknd - Blinding Lights (Official Audio)", "The Weeknd - Blinding Lights (Official Audio)"),
        ("Lofi Girl | lofi hip hop radio - YouTube", "Lofi Girl | lofi hip hop radio"),
        ("Top 10 - YouTuber fails - YouTube", "Top 10 - YouTuber fails"),
        ("x" * 300, "x" * 120),
    ],
)
def test_result_titles_are_single_and_capped(raw, title):
    assert youtube.result_title(raw) == title


@pytest.mark.parametrize(
    "snippet, channel",
    [
        ("YouTube · Tie Guy · 12M views", "Tie Guy"),
        ("Video uploaded by Real Men Real Style. Learn how", "Real Men Real Style"),
        ("Learn to tie a tie by the easiest method", None),
        ("", None),
    ],
)
def test_channel_only_when_stated(snippet, channel):
    assert youtube.result_channel(snippet) == channel
