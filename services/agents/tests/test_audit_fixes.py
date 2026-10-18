"""Regression tests for the bugs found in the agent audit."""

import pytest
from laai_agents import live, news, youtube
from laai_agents.imessage import tidy
from laai_agents.models import AlternativesRequest, PriceCompareResponse, YouTubeRequest
from laai_agents.search import SearchUnavailable


# 1. The brief kept news with "deal" in it; only shopping deal pages are not stories.
@pytest.mark.parametrize(
    "title, story",
    [
        ("Israel and Hamas reach ceasefire deal", True),
        ("Trump signs trade deal with China", True),
        ("Senate passes budget deal to avert shutdown", True),
        ("Best deals on AirPods this weekend", False),
        ("Black Friday deals you can shop now", False),
        ("Deal of the day: Sony headphones", False),
    ],
)
def test_deal_news_is_kept_but_deal_pages_are_not(title, story):
    assert (news.NOT_A_STORY.search(title) is None) is story


# 2. A channel named only "Official" or "VEVO" must not match every creator.
@pytest.mark.parametrize("channel", ["Official", "VEVO", "Official VEVO", " - Topic"])
def test_empty_channel_names_match_no_one(channel):
    assert not youtube.same_creator(channel, "Markiplier")


# 3. "by" inside a song title is not the artist separator.
@pytest.mark.parametrize(
    "query, title, creator",
    [
        ("Stand by Me", "Stand by Me", None),
        ("Stand by Me by Ben E. King", "Stand by Me", "Ben E. King"),
        ("Blinding Lights by The Weeknd", "Blinding Lights", "The Weeknd"),
        ("Toxic by BoywithUke", "Toxic", "BoywithUke"),
    ],
)
def test_song_titles_with_by(query, title, creator):
    want = youtube.intent(query, "song")
    assert (want["title"], want["creator"]) == (title, creator)


# 6. The message formatter must not break links, brand casing or short texts.
@pytest.mark.parametrize(
    "text, expected",
    [
        ("https://youtu.be/dQw4w9WgXcQ", "https://youtu.be/dQw4w9WgXcQ"),
        ("check this out https://youtu.be/dQw4w9WgXcQ", "Check this out https://youtu.be/dQw4w9WgXcQ"),
        ("iPhone pickup at 5", "iPhone pickup at 5."),
        ("ok", "Ok"),
        ("running 10 minutes late", "Running 10 minutes late."),
        ("see you at 7 :)", "See you at 7 :)"),
    ],
)
def test_message_formatting_keeps_links_and_short_texts(text, expected):
    assert tidy(text) == expected


# 7. A failed category search must not sink the alternatives agent.
async def test_alternatives_survive_a_failed_category_search(monkeypatch):
    async def down(query):
        raise SearchUnavailable("rate limited")

    async def no_price(req):
        return PriceCompareResponse(product=req.product, offers=[], summary="none")

    monkeypatch.setattr(live, "search", down)
    monkeypatch.setattr(live, "compare", no_price)
    result = await live.alternatives(AlternativesRequest(product="Sony XM5", max_price=300))
    assert result.alternatives  # catalog candidates still listed
    assert result.data_source == "unavailable" and result.error is None


# 8. A "latest" request during a search outage says so instead of "no video found".
async def test_latest_reports_search_outage_not_missing_video(monkeypatch):
    async def outage(creator):
        raise SearchUnavailable("rate limited")

    async def nothing(query, *extra):
        return []

    monkeypatch.delenv("YOUTUBE_API_KEY", raising=False)
    monkeypatch.delenv("SERPAPI_API_KEY", raising=False)
    monkeypatch.setattr(youtube, "latest_from_feed", outage)
    monkeypatch.setattr(youtube, "PROVIDERS", (("ddgs_videos", nothing), ("web_search", nothing)))
    result = await youtube.find_video(YouTubeRequest(query="find markipliers latest video", kind="video"))
    assert result.error == "YouTube search unavailable"
    assert "temporarily unavailable" in result.summary
