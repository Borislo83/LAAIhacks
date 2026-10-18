import logging
import uuid

import aiohttp
import pytest

from laai_agents import news, progress
from laai_agents.models import NewsRequest

KEY = "tvly-secret-test-key-123"
LONG = "The city council approved the transit budget on Tuesday. " * 15


def result(n, title=None, content="Short snippet.", raw=LONG, images=None, date=None):
    return {
        "url": f"https://n{n}.example/story",
        "title": title or f"Council approves transit budget {n}",
        "content": content,
        "raw_content": raw,
        "score": 0.9,
        "published_date": date,
        "images": images or [],
    }


class FakeResponse:
    def __init__(self, status, data):
        self.status, self.data = status, data

    async def json(self):
        if isinstance(self.data, Exception):
            raise self.data
        return self.data

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False


class FakeSession:
    def __init__(self, reply):
        self.reply, self.posts = reply, []

    def post(self, url, **kwargs):
        self.posts.append((url, kwargs))
        if isinstance(self.reply, Exception):
            raise self.reply
        return FakeResponse(*self.reply)


@pytest.fixture
def tavily(monkeypatch):
    """Fake Tavily, DDGS and pages: no network. `state["reply"]` is (status, json) or an error."""
    monkeypatch.setenv("TAVILY_API_KEY", KEY)
    monkeypatch.setattr(news.llm, "has_key", lambda: False)
    state = {
        "reply": (200, {"results": []}),
        "ddgs": [],
        "pages": {},
        "fetched": [],
        "sessions": [],
    }

    def session():
        fake = FakeSession(state["reply"])
        state["sessions"].append(fake)
        return fake

    async def ddgs(query):
        state["ddgs_called"] = True
        return state["ddgs"]

    async def web(query):
        return []

    async def fetch(url):
        state["fetched"].append(url)
        return state["pages"].get(url)

    monkeypatch.setattr(news.net, "session", session)
    monkeypatch.setattr(news, "PROVIDERS", (("ddgs_news", ddgs), ("web_search", web)))
    monkeypatch.setattr(news, "fetch_page", fetch)
    return state


async def test_request_shape(tavily):
    tavily["reply"] = (200, {"results": [result(1)]})
    await news.tavily_news("council budget", 6)
    [(url, kwargs)] = tavily["sessions"][0].posts
    assert url == "https://api.tavily.com/search"
    assert kwargs["headers"]["Authorization"] == f"Bearer {KEY}"
    assert kwargs["timeout"].total == 15
    assert kwargs["json"] == {
        "query": "council budget",
        "topic": "news",
        "time_range": "week",
        "max_results": 6,
        "search_depth": "basic",
        "include_raw_content": "text",
        "include_images": True,
        "include_image_descriptions": True,
        "include_answer": False,
    }
    # The key travels only in the header.
    assert KEY not in str(kwargs["json"]) and KEY not in url


async def test_results_map_to_candidates(tavily):
    tavily["reply"] = (
        200,
        {
            "results": [
                result(
                    1,
                    content="Lead from Tavily.",
                    raw="  Full   text\n of the article.  " + LONG,
                    date="Thu, 16 Oct 2026 09:00:00 GMT",
                    images=[
                        {
                            "url": "https://img.example/a.jpg",
                            "description": "Council chamber",
                        }
                    ],
                ),
                result(2, raw=None, content="Only a snippet here."),
                {"url": "http://insecure.example/x", "title": "Insecure"},
                "not a dict",
            ]
        },
    )
    first, second = await news.tavily_news("budget", 8)
    assert first["url"] == "https://n1.example/story"
    assert first["lead"] == "Lead from Tavily."
    assert first["text"].startswith("Full text of the article.")
    assert first["publisher"] is None
    assert first["published_at"] == "2026-10-16T09:00:00+00:00"
    assert (first["image"], first["image_alt"]) == (
        "https://img.example/a.jpg",
        "Council chamber",
    )
    assert second["text"] == "Only a snippet here." and second["image"] is None


async def test_images_come_from_tavily_results_and_must_be_public_https(tavily):
    tavily["reply"] = (
        200,
        {
            "results": [
                result(
                    1, images=["http://img.example/plain.jpg", "https://10.0.0.1/a.jpg"]
                ),
                result(
                    2,
                    title="Transit riders react to spending plan",
                    images=[
                        "https://img.example/logo.png",
                        "https://img.example/hero.jpg",
                    ],
                ),
            ]
        },
    )
    out = await news.summarize_news(NewsRequest(query="council transit budget"))
    assert out.image_url == "https://img.example/hero.jpg"
    assert out.image_source_url == "https://n2.example/story"
    assert out.image_alt == "Transit riders react to spending plan"
    # Enough Tavily text and an image: no page was fetched, and DDGS was never asked.
    assert tavily["fetched"] == [] and "ddgs_called" not in tavily


async def test_thin_results_fetch_their_page(tavily):
    tavily["reply"] = (
        200,
        {
            "results": [
                result(1, images=["https://img.example/hero.jpg"]),
                result(2, title="Budget vote details", raw=None, content="Thin."),
            ]
        },
    )
    tavily["pages"]["https://n2.example/story"] = (
        '<html><head><meta property="og:site_name" content="City Times"></head><body>'
        f"<article><p>{LONG}</p></article></body></html>"
    )
    out = await news.summarize_news(NewsRequest(query="council transit budget"))
    assert tavily["fetched"] == ["https://n2.example/story"]
    assert "City Times" in [s.publisher for s in out.sources]


async def test_no_images_fetches_top_pages_for_og_image(tavily):
    tavily["reply"] = (
        200,
        {"results": [result(i, title=f"Budget story {i}") for i in range(5)]},
    )
    tavily["pages"]["https://n0.example/story"] = (
        '<html><head><meta property="og:image" content="/hero.jpg"></head><body></body></html>'
    )
    out = await news.summarize_news(NewsRequest(query="budget"))
    assert sorted(tavily["fetched"]) == [
        f"https://n{i}.example/story" for i in range(3)
    ]
    assert out.image_url == "https://n0.example/hero.jpg"


async def test_progress_names_tavily(tavily):
    tavily["reply"] = (
        200,
        {"results": [result(1, images=["https://img.example/a.jpg"])]},
    )
    progress_id = "t" * 32
    await news.summarize_news(
        NewsRequest(query="council budget", progress_id=progress_id)
    )
    events = progress.read(progress_id, 0)
    assert "Searching news (Tavily) for council budget" in [
        e["message"] for e in events
    ]
    assert [e["stage"] for e in events] == [
        "started",
        "searching",
        "ranking",
        "summarizing",
        "completed",
    ]


DDGS_HIT = {
    "url": "https://free.example/story",
    "title": "Council approves budget",
    "lead": "Free search lead.",
    "publisher": "Free News",
    "published_at": None,
}


@pytest.mark.parametrize(
    "reply, reason",
    [
        ((401, {"detail": {"error": f"Unauthorized: {KEY}"}}), "HTTP 401"),
        ((429, {"detail": "rate limited"}), "HTTP 429"),
        ((432, {}), "HTTP 432"),
        (TimeoutError(), "request failed"),
        (aiohttp.ClientConnectionError(f"cannot connect {KEY}"), "request failed"),
        ((200, ValueError("not json")), "request failed"),
        ((200, {"results": []}), None),
        ((200, ["unexpected"]), "unexpected response"),
    ],
)
async def test_falls_back_to_ddgs(tavily, caplog, reply, reason):
    caplog.set_level(logging.DEBUG)
    tavily["reply"] = reply
    tavily["ddgs"] = [dict(DDGS_HIT)]
    progress_id = uuid.uuid4().hex
    out = await news.summarize_news(
        NewsRequest(query="council budget", progress_id=progress_id)
    )
    assert out.error is None and out.headline == "Council approves budget"
    assert out.sources[0].url == "https://free.example/story"
    messages = [e["message"] for e in progress.read(progress_id, 0)]
    fallback = next(m for m in messages if "Tavily" in m and "free news search" in m)
    assert (reason or "found nothing") in fallback
    assert "Searching news (DDGS) for council budget" in messages
    # The key never reaches progress events, the response or logs.
    assert KEY not in str(progress.read(progress_id, 0))
    assert KEY not in out.json() and KEY not in caplog.text


async def test_no_key_skips_tavily(tavily, monkeypatch):
    monkeypatch.delenv("TAVILY_API_KEY")
    tavily["ddgs"] = [dict(DDGS_HIT)]
    out = await news.summarize_news(NewsRequest(query="council budget"))
    assert tavily["sessions"] == [] and out.headline == "Council approves budget"
    with pytest.raises(news.TavilyUnavailable):
        await news.tavily_news("q", 5)


async def test_tavily_error_message_never_carries_the_key(tavily):
    tavily["reply"] = (401, {"detail": KEY})
    with pytest.raises(news.TavilyUnavailable) as caught:
        await news.tavily_news("q", 5)
    assert str(caught.value) == "Tavily returned HTTP 401"


async def test_everything_failing_is_still_an_unavailable_error(tavily, monkeypatch):
    tavily["reply"] = (429, {})

    async def down(query):
        raise news.SearchUnavailable("down")

    monkeypatch.setattr(news, "PROVIDERS", (("ddgs_news", down), ("web_search", down)))
    out = await news.summarize_news(NewsRequest(query="council budget"))
    assert out.error == "News search unavailable" and out.headline is None
