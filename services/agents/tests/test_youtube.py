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
    monkeypatch.delenv("SERPAPI_API_KEY", raising=False)
    calls = {"ddgs": [], "web": []}
    results = {"ddgs": [], "web": []}

    def fake(name):
        async def run(query, *extra):
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
    # The keyless channel lookups are network calls too; keep these tests offline.
    async def no_channel(url):
        return None, None

    async def no_feed(creator):
        return []

    monkeypatch.setattr(youtube, "oembed_channel", no_channel)
    monkeypatch.setattr(youtube, "latest_from_feed", no_feed)
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

    async def api(query, want=None):
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
        {"url": WATCH, "title": "Real video", "channel": "Channel", "duration": "3:21",
         "published": None, "views": None}
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


def _v(title, channel, published=None, views=None, vid="a"):
    from laai_agents.youtube import _video

    return _video(f"https://www.youtube.com/watch?v={vid * 11}"[:43], title, channel,
                  published=published, views=views)


def test_official_artist_upload_beats_reuploads():
    from laai_agents.youtube import intent, rank

    want = intent("Toxic by BoywithUke", "song")
    videos = [
        _v("BoyWithUke - Toxic (Audio Oficial)", "Musica Hits Latino", views=900000, vid="a"),
        _v("Toxic - BoyWithUke (slowed + reverb)", "Chill Edits", views=5000000, vid="b"),
        _v("Toxic", "BoyWithUke - Topic", views=200000000, vid="c"),
        _v("BoyWithUke - Toxic (Official Lyric Video)", "BoyWithUke", views=150000000, vid="d"),
    ]
    best = rank(videos, want, "Toxic by BoywithUke")[0]
    assert best["channel"] in {"BoyWithUke - Topic", "BoyWithUke"}


def test_latest_means_newest_upload_from_that_creator():
    from laai_agents.youtube import intent, rank

    want = intent("markipiliers latest", "video")
    assert want["creator"] == "markipiliers" and want["recent"]
    videos = [
        _v("ATTACK OF THE MARKIPLIERS", "Markiplier", "2016-03-01T00:00:00", 9000000, "a"),
        _v("Markiplier reacts compilation", "Fan Channel", "2026-10-16T00:00:00", 1000, "b"),
        _v("New video this week", "Markiplier", "2026-10-12T00:00:00", 2000000, "c"),
        _v("Older video", "Markiplier", "2026-09-01T00:00:00", 3000000, "d"),
    ]
    best = rank(videos, want, "markipiliers latest")[0]
    assert (best["title"], best["channel"]) == ("New video this week", "Markiplier")


def test_creator_names_match_loosely_but_not_wrongly():
    from laai_agents.youtube import intent, same_creator

    assert same_creator("Markiplier", "markipiliers")
    assert same_creator("BoyWithUke - Topic", "boywithuke") and same_creator("BoyWithUkeVEVO", "BoyWithUke")
    assert not same_creator("Musica Hits Latino", "BoyWithUke")
    assert intent("MrBeast's newest", "video")["creator"] == "MrBeast"


async def test_data_api_latest_uses_the_channels_uploads_by_date(monkeypatch):
    from laai_agents import youtube

    monkeypatch.setenv("YOUTUBE_API_KEY", "k")
    seen = []

    async def api_get(path, params):
        seen.append(params)
        if params["type"] == "channel":
            return {"items": [{"id": {"channelId": "UC123"}, "snippet": {"title": "Markiplier"}}]}
        return {"items": [{"id": {"videoId": "abcdefghijk"},
                           "snippet": {"title": "Newest", "channelTitle": "Markiplier",
                                       "publishedAt": "2026-10-16T00:00:00Z"}}]}

    monkeypatch.setattr(youtube, "_api_get", api_get)
    videos = await youtube.api_search("markipiliers", youtube.intent("markipiliers latest", "video"))
    assert seen[1]["channelId"] == "UC123" and seen[1]["order"] == "date"
    assert videos[0]["title"] == "Newest" and videos[0]["published"].startswith("2026-10-16")


FEED = """<?xml version="1.0" encoding="UTF-8"?>
<feed xmlns:yt="http://www.youtube.com/xml/schemas/2015" xmlns:media="http://search.yahoo.com/mrss/"
      xmlns="http://www.w3.org/2005/Atom">
 <entry><yt:videoId>NEWNEWNEW01</yt:videoId><title>Newest upload</title>
  <author><name>Markiplier</name></author><published>2026-10-16T17:00:00+00:00</published>
  <media:group><media:community><media:statistics views="123456"/></media:community></media:group></entry>
 <entry><yt:videoId>OLDOLDOLD01</yt:videoId><title>Older upload</title>
  <author><name>Markiplier</name></author><published>2026-10-01T17:00:00+00:00</published></entry>
</feed>"""


def test_feed_parsing_keeps_dates_channel_and_views():
    videos = youtube.parse_feed(FEED)
    assert [v["title"] for v in videos] == ["Newest upload", "Older upload"]
    assert videos[0]["channel"] == "Markiplier" and videos[0]["views"] == 123456
    assert videos[0]["url"] == "https://www.youtube.com/watch?v=NEWNEWNEW01"
    assert youtube.parse_feed("not xml") == []


async def test_latest_picks_the_real_channel_over_lookalikes(monkeypatch):
    from laai_agents.models import Source

    async def search(query):
        sources = [
            Source(title="markiplier - YouTube", url="https://www.youtube.com/@markiplier2278"),
            Source(title="Markiplier - Topic - YouTube",
                   url="https://www.youtube.com/channel/UC69Q77DOizOJ-K2hT5COLOQ"),
            Source(title="Markiplier - YouTube",
                   url="https://www.youtube.com/channel/UC7_YxT-KID8kRbqZo7MyscQ"),
            Source(title="Markiplier", url="https://music.youtube.com/channel/UC7_YxT-KID8kRbqZo7MyscQ"),
        ]
        return sources, {}

    fetched = []

    async def fetch(url, params=None):
        fetched.append(url)
        return FEED if "UC7_YxT-KID8kRbqZo7MyscQ" in url else None

    monkeypatch.setattr(youtube, "search", search)
    monkeypatch.setattr(youtube, "_fetch_text", fetch)
    uploads = await youtube.latest_from_feed("markipiliers")
    assert uploads[0]["title"] == "Newest upload"
    assert fetched[0].endswith("channel_id=UC7_YxT-KID8kRbqZo7MyscQ")  # real channel tried first


async def test_oembed_fills_in_the_uploading_channel(monkeypatch):
    async def fetch(url, params=None):
        assert url == "https://www.youtube.com/oembed"
        return '{"author_name": "BoyWithUke - Topic", "author_url": "https://www.youtube.com/channel/UCaaaaaaaaaaaaaaaaaaaaaa"}'

    monkeypatch.setattr(youtube, "_fetch_text", fetch)
    monkeypatch.setattr(youtube, "_oembed", {})
    videos = await youtube.with_channels([youtube._video(WATCH, "Toxic")])
    assert videos[0]["channel"] == "BoyWithUke - Topic"
    assert videos[0]["channel_url"].endswith("UCaaaaaaaaaaaaaaaaaaaaaa")


def test_channel_links_must_be_youtube():
    assert youtube.CHANNEL_URL.match("https://www.youtube.com/@Markiplier/videos")
    assert not youtube.CHANNEL_URL.match("https://www.youtube.com.evil.example/@x")
    assert not youtube.CHANNEL_URL.match("http://www.youtube.com/@x")


async def test_data_api_channel_result_without_an_id_is_skipped(monkeypatch):
    from laai_agents import youtube

    monkeypatch.setenv("YOUTUBE_API_KEY", "k")

    async def api_get(path, params):
        if params["type"] == "channel":
            return {"items": [{"id": {}, "snippet": {"title": "Markiplier"}}]}
        return {"items": [{"id": {"videoId": "abcdefghijk"},
                           "snippet": {"title": "Search hit", "channelTitle": "Markiplier"}}]}

    monkeypatch.setattr(youtube, "_api_get", api_get)
    videos = await youtube.api_search("markiplier", youtube.intent("markiplier latest", "video"))
    assert videos[0]["title"] == "Search hit"
