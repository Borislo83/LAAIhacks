from datetime import UTC, datetime, timedelta

import pytest

from laai_agents import news, progress
from laai_agents.agents import build_news_agent
from laai_agents.models import NewsRequest, NewsResponse, Source

NOW = datetime(2026, 10, 17, 12, tzinfo=UTC)
_real_ddgs_news = news.ddgs_news
_real_web_news = news.web_news


def page(
    title, description="", image=None, width=None, site=None, published=None, body=""
):
    meta = [f'<meta property="og:title" content="{title}">']
    if description:
        meta.append(f'<meta property="og:description" content="{description}">')
    if image:
        meta.append(f'<meta property="og:image" content="{image}">')
    if width:
        meta.append(f'<meta property="og:image:width" content="{width}">')
    if site:
        meta.append(f'<meta property="og:site_name" content="{site}">')
    if published:
        meta.append(f'<meta property="article:published_time" content="{published}">')
    return (
        f"<html><head><title>ignored</title>{''.join(meta)}</head><body>"
        f"<nav><p>Subscribe to our newsletter for the best stories every day.</p></nav>"
        f"<article>{body}</article><script>var x = 'not text at all, never';</script>"
        f"</body></html>"
    )


def hit(url, title, lead="", publisher=None, published=None):
    return news._hit(url, title, lead, publisher, published)


def article(url, title, lead="", publisher="Example", hours_old=None, **extra):
    published = (
        (NOW - timedelta(hours=hours_old)).isoformat()
        if hours_old is not None
        else None
    )
    return {
        "url": url,
        "title": title,
        "lead": lead,
        "publisher": publisher,
        "published_at": published,
        "text": "",
        "image": None,
        "image_alt": None,
        "image_width": None,
        "image_height": None,
        **extra,
    }


@pytest.fixture
def world(monkeypatch):
    """Fake search and pages: no network. `pages` maps URL -> HTML (missing = unreadable)."""
    monkeypatch.setattr(news.llm, "has_key", lambda: False)
    monkeypatch.delenv("TAVILY_API_KEY", raising=False)
    state = {"ddgs": [], "web": [], "pages": {}, "fetched": [], "queries": []}

    def provider(name):
        async def run(query):
            state["queries"].append((name, query))
            value = state[name]
            if isinstance(value, Exception):
                raise value
            return value

        return run

    async def fetch(url):
        state["fetched"].append(url)
        return state["pages"].get(url)

    monkeypatch.setattr(
        news,
        "PROVIDERS",
        (("ddgs_news", provider("ddgs")), ("web_search", provider("web"))),
    )
    monkeypatch.setattr(news, "fetch_page", fetch)
    return state


@pytest.mark.parametrize(
    "text, expected",
    [
        (
            "what's the latest on the Mars sample return mission?",
            "the Mars sample return mission",
        ),
        ("news about Taylor Swift", "Taylor Swift"),
        ("Can you give me the latest news on SpaceX Starship", "SpaceX Starship"),
        ("any updates on the LA Metro strike today?", "the LA Metro strike"),
        ("what's happening with Nvidia earnings", "Nvidia earnings"),
        ("give me a quick summary of the news about the Fed", "the Fed"),
        ("OpenAI news", "OpenAI"),
        ("Dodgers", "Dodgers"),
        ("what's the news?", "what's the news?"),
    ],
)
def test_request_phrasing_is_stripped(text, expected):
    assert news.parse_query(text) == expected


def test_rank_prefers_title_overlap_and_recency_and_drops_syndicated_copies():
    articles = [
        article(
            "https://a.example/old", "Fed holds interest rates steady", hours_old=150
        ),
        article(
            "https://b.example/x", "Local bakery opens second shop", "Bread and cake."
        ),
        article(
            "https://c.example/new",
            "Fed holds interest rates steady again",
            hours_old=2,
        ),
        article(
            "https://d.example/syn",
            "Fed holds interest rates steady again",
            "AP copy",
            publisher="Other",
            hours_old=1,
        ),
        article(
            "https://c.example/new?utm_source=x",
            "Different title, same URL",
            "Fed rates",
        ),
        article(
            "https://e.example/lead",
            "Markets today",
            "The Fed kept interest rates on hold.",
            hours_old=3,
        ),
    ]
    ranked = news.rank(articles, "Fed interest rates", now=NOW)
    urls = [a["url"] for a in ranked]
    # Newest copy of the syndicated story wins; the copy and the tracking-URL duplicate go.
    assert urls[0] == "https://d.example/syn"
    assert (
        "https://c.example/new" not in urls
        and "https://c.example/new?utm_source=x" not in urls
    )
    # The older copy of the same story goes too.
    assert "https://a.example/old" not in urls
    # Off-topic article is dropped once on-topic ones exist.
    assert "https://b.example/x" not in urls


def test_rank_mixes_publishers_lightly():
    articles = [
        article(
            "https://big.example/1",
            "Storm hits coast hard",
            publisher="Big",
            hours_old=1,
        ),
        article(
            "https://big.example/2",
            "Coast storm damage counted",
            publisher="Big",
            hours_old=1,
        ),
        article(
            "https://small.example/1",
            "Coast storm power cuts",
            publisher="Small",
            hours_old=1,
        ),
    ]
    ranked = news.rank(articles, "coast storm", now=NOW)
    assert [a["publisher"] for a in ranked] == ["Big", "Small", "Big"]


@pytest.mark.parametrize(
    "url, width, ok",
    [
        ("https://cdn.example.com/photo.jpg", 1200, True),
        ("https://cdn.example.com/photo.jpg", None, True),
        ("http://cdn.example.com/photo.jpg", 1200, False),
        ("https://cdn.example.com/photo.jpg", 120, False),
        ("https://cdn.example.com/static/logo.png", None, False),
        ("https://127.0.0.1/photo.jpg", None, False),
        ("https://10.0.0.5/photo.jpg", None, False),
        ("https://169.254.169.254/latest/meta-data", None, False),
        ("https://[::1]/photo.jpg", None, False),
        ("https://localhost/photo.jpg", None, False),
        ("https://user@cdn.example.com/photo.jpg", None, False),
        ("javascript:alert(1)", None, False),
        (None, None, False),
    ],
)
def test_image_must_be_public_https_and_not_tiny(url, width, ok):
    assert news.valid_image(url, width) is ok


def test_image_comes_from_most_relevant_article_with_a_valid_one():
    ranked = [
        article("https://a.example/1", "Top story", image="http://a.example/x.jpg"),
        article("https://b.example/2", "Second", image="https://b.example/logo.png"),
        article(
            "https://c.example/3",
            "Third",
            image="https://c.example/big.jpg",
            image_alt="Crowd",
        ),
        article("https://d.example/4", "Fourth", image="https://d.example/other.jpg"),
    ]
    assert news.pick_image(ranked) == (
        "https://c.example/big.jpg",
        "Crowd",
        "https://c.example/3",
    )
    assert news.pick_image(ranked[:2]) is None


def test_extract_reads_meta_and_main_text_only():
    body = (
        "<p>"
        + "The council voted to approve the new transit budget on Tuesday. " * 2
        + "</p><p>short</p>"
    )
    info = news.extract(
        page(
            "Council approves budget",
            "Lead text.",
            "/img/hero.jpg",
            1200,
            "City Times",
            "2026-10-16T09:00:00Z",
            body,
        ),
        "https://city.example/news/budget",
    )
    assert info["title"] == "Council approves budget"
    assert info["image"] == "https://city.example/img/hero.jpg"
    assert info["image_width"] == 1200
    assert info["publisher"] == "City Times"
    assert info["published_at"] == "2026-10-16T09:00:00+00:00"
    assert info["description"] == "Lead text."
    assert "transit budget" in info["text"]
    assert "newsletter" not in info["text"] and "short" not in info["text"]
    assert "never" not in info["text"]


class FakeContent:
    def __init__(self, total, chunk=65536):
        self.total, self.chunk, self.served = total, chunk, 0

    async def iter_chunked(self, size):
        while self.served < self.total:
            n = min(self.chunk, self.total - self.served)
            self.served += n
            yield b"<p>" + b"x" * (n - 3)


class FakeResponse:
    def __init__(self, status=200, content_type="text/html", total=1000, location=None):
        self.status = status
        self.headers = {"Content-Type": content_type}
        if location:
            self.headers["Location"] = location
        self.content = FakeContent(total)

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False


class FakeSession:
    def __init__(self, responses):
        self.responses, self.requested = responses, []

    def get(self, url, **kwargs):
        self.requested.append(url)
        return self.responses[url]


@pytest.fixture
def http(monkeypatch):
    async def public(host):
        return host not in {"internal.example", "localhost"}

    def install(responses):
        session = FakeSession(responses)
        monkeypatch.setattr(news.net, "session", lambda: session)
        return session

    monkeypatch.setattr(news, "public_host", public)
    return install


async def test_page_reads_stop_at_the_size_cap(http):
    session = http({"https://big.example/a": FakeResponse(total=5_000_000)})
    body = await news.fetch_page("https://big.example/a")
    assert len(body.encode()) == news.PAGE_LIMIT
    assert session.responses["https://big.example/a"].content.served < 5_000_000


async def test_page_fetch_refuses_http_private_hosts_and_bad_redirects(http):
    session = http(
        {
            "https://ok.example/a": FakeResponse(
                status=302, location="https://internal.example/x"
            ),
            "https://ok.example/b": FakeResponse(
                status=301, location="http://ok.example/c"
            ),
            "https://ok.example/pdf": FakeResponse(content_type="application/pdf"),
        }
    )
    assert await news.fetch_page("http://ok.example/a") is None
    assert await news.fetch_page("https://internal.example/a") is None
    assert await news.fetch_page("https://ok.example:8443/a") is None
    assert await news.fetch_page("https://ok.example/a") is None
    assert await news.fetch_page("https://ok.example/b") is None
    assert await news.fetch_page("https://ok.example/pdf") is None
    assert session.requested == [
        "https://ok.example/a",
        "https://ok.example/b",
        "https://ok.example/pdf",
    ]


async def test_public_host_check_resolves_names(monkeypatch):
    from laai_agents import merchants

    async def resolve(host, port):
        return [(None, None, None, None, ("10.1.2.3", 443))]

    class Loop:
        getaddrinfo = staticmethod(resolve)

    monkeypatch.setattr(merchants.asyncio, "get_running_loop", lambda: Loop())
    assert not await merchants.public_host("sneaky.example")


async def test_llm_prompt_contains_only_the_top_articles_text(monkeypatch):
    seen = {}

    async def complete(system, user, timeout=30.0):
        seen["system"], seen["user"] = system, user
        return "The council approved the budget. Two members voted against it."

    monkeypatch.setattr(news.llm, "has_key", lambda: True)
    monkeypatch.setattr(news.llm, "complete", complete)
    ranked = [
        article(
            f"https://s{i}.example/a",
            f"Story {i}",
            f"Lead {i}.",
            publisher=f"Pub{i}",
            text=f"Body text {i}.",
        )
        for i in range(7)
    ]
    summary = await news.llm_summary(ranked)
    assert summary == "The council approved the budget. Two members voted against it."
    assert seen["user"] == news.source_text(ranked)
    expected = "\n\n".join(
        f"[{i + 1}] Pub{i}: Story {i}\nBody text {i}." for i in range(5)
    )
    assert seen["user"] == expected
    assert "Story 5" not in seen["user"] and "https://" not in seen["user"]
    for rule in ("only facts that appear", "Do not speculate", "2 to 4", "disagree"):
        assert rule in seen["system"]


async def test_llm_summary_with_an_unsupported_number_is_discarded(monkeypatch):
    async def complete(system, user, timeout=30.0):
        return "The budget is $4.2 billion. It passed."

    monkeypatch.setattr(news.llm, "has_key", lambda: True)
    monkeypatch.setattr(news.llm, "complete", complete)
    ranked = [
        article(
            "https://a.example/1",
            "Budget passes",
            text="The budget passed with 7 votes.",
        )
    ]
    assert await news.llm_summary(ranked) is None


async def test_llm_summary_is_trimmed_to_four_sentences_and_600_chars(monkeypatch):
    async def complete(system, user, timeout=30.0):
        return "**Summary:** " + " ".join(
            f"Sentence number {w} is here." for w in "abcdef"
        )

    monkeypatch.setattr(news.llm, "has_key", lambda: True)
    monkeypatch.setattr(news.llm, "complete", complete)
    summary = await news.llm_summary([article("https://a.example/1", "T", text="x")])
    assert summary.count(".") == 4 and len(summary) <= 600 and "*" not in summary


def test_extractive_fallback_uses_lead_sentences_verbatim():
    ranked = [
        article(
            "https://a.example/1",
            "Budget passes",
            "The city council approved a $2.1 billion budget on Tuesday. The vote was 9 to 4. "
            "More details soon",
        ),
        article(
            "https://b.example/2",
            "Budget vote",
            "The city council approved a $2.1 billion budget Tuesday.",
        ),
        article(
            "https://c.example/3",
            "Unions react",
            "Transit unions said the plan protects bus service. Riders also said...",
        ),
    ]
    summary = news.extractive_summary(ranked)
    assert summary == (
        "The city council approved a $2.1 billion budget on Tuesday. The vote was 9 to 4. "
        "Transit unions said the plan protects bus service."
    )
    assert len(summary) <= 600


async def test_full_run_without_llm(world):
    world["ddgs"] = [
        hit(
            "https://news.example/budget",
            "Council approves budget",
            "Snippet.",
            "City Times",
            "2026-10-16T09:00:00Z",
        ),
        hit(
            "https://wire.example/budget?ref=feed",
            "Council approves budget",
            "Wire copy.",
            "Wire",
        ),
        hit("https://other.example/sports", "Dodgers win", "Baseball.", "Sports Daily"),
    ]
    world["pages"]["https://news.example/budget"] = page(
        "Council approves transit budget",
        "The council approved the transit budget in a 9 to 4 vote. Bus service is protected.",
        "https://img.example/hero.jpg",
        1200,
        "City Times",
    )
    progress_id = "p" * 32
    result = await news.summarize_news(
        NewsRequest(
            query="what's the latest on the council budget?", progress_id=progress_id
        )
    )
    assert result.query == "the council budget"
    assert result.error is None
    assert result.headline == "Council approves transit budget"
    assert result.summary == (
        "The council approved the transit budget in a 9 to 4 vote. Bus service is protected."
    )
    assert result.image_url == "https://img.example/hero.jpg"
    assert result.image_source_url == "https://news.example/budget"
    assert [s.url for s in result.sources] == ["https://news.example/budget"]
    assert result.sources[0].publisher == "City Times"
    assert world["queries"] == [("ddgs", "the council budget")]
    stages = [e["stage"] for e in progress.read(progress_id, 0)]
    assert stages == [
        "started",
        "searching",
        "reading",
        "ranking",
        "summarizing",
        "completed",
    ]
    assert "Reading 3 articles" in [e["message"] for e in progress.read(progress_id, 0)]


async def test_reads_at_most_max_sources_pages(world):
    world["ddgs"] = [
        hit(f"https://n{i}.example/a", f"Storm update {i}", "Storm.") for i in range(12)
    ]
    result = await news.summarize_news(NewsRequest(query="storm", max_sources=3))
    assert len(world["fetched"]) == 3
    assert len(result.sources) <= 3


async def test_falls_back_to_web_search(world):
    world["ddgs"] = news.SearchUnavailable("down")
    world["web"] = [
        hit("https://n.example/a", "Election results are in", "Votes were counted.")
    ]
    result = await news.summarize_news(NewsRequest(query="news about the election"))
    assert result.headline == "Election results are in"
    assert world["queries"] == [("ddgs", "the election"), ("web", "the election")]


async def test_web_search_adds_news_to_query(monkeypatch):
    calls = []

    async def search(query):
        calls.append(query)
        return [
            Source(title="T", url="https://a.example/x", snippet="s"),
            Source(title="T", url="http://a.example/y"),
        ], {}

    monkeypatch.setattr(news, "search", search)
    hits = await _real_web_news("mars")
    assert calls == ["mars news"] and [h["url"] for h in hits] == [
        "https://a.example/x"
    ]


async def test_ddgs_rows_are_mapped(monkeypatch):
    import ddgs

    class FakeDDGS:
        def __init__(self, timeout):
            pass

        def news(self, query, **kwargs):
            assert kwargs["timelimit"] == "w"
            return [
                {
                    "url": "https://a.example/1",
                    "title": "T",
                    "body": "B",
                    "source": "AP",
                    "date": "2026-10-16T09:00:00+00:00",
                    "image": "https://a.example/i.jpg",
                },
                {"url": "http://a.example/2", "title": "Insecure"},
            ]

    monkeypatch.setattr(ddgs, "DDGS", FakeDDGS)
    assert await _real_ddgs_news("q") == [
        {
            "url": "https://a.example/1",
            "title": "T",
            "lead": "B",
            "publisher": "AP",
            "published_at": "2026-10-16T09:00:00+00:00",
        }
    ]


async def test_empty_results(world):
    result = await news.summarize_news(NewsRequest(query="zzqx nothing"))
    assert result.error is None and result.headline is None and result.sources == []
    assert result.summary == "No recent news found for zzqx nothing."
    assert result.image_url is None


async def test_search_error(world):
    world["ddgs"] = news.SearchUnavailable("down")
    world["web"] = news.SearchUnavailable("down")
    result = await news.summarize_news(NewsRequest(query="mars"))
    assert result.error == "News search unavailable"
    assert result.headline is None and result.sources == []
    assert "unavailable" in result.summary


def test_request_limits():
    assert NewsRequest(query="x").max_sources == 8
    for bad in (
        {"query": ""},
        {"query": "x" * 301},
        {"query": "x", "max_sources": 2},
        {"query": "x", "max_sources": 13},
    ):
        with pytest.raises(ValueError):
            NewsRequest(**bad)


def test_chat_reply_is_plain_text_with_three_links():
    result = NewsResponse(
        query="q",
        headline="Headline",
        summary="One. Two.",
        sources=[
            {"title": f"T{i}", "url": f"https://s{i}.example/a", "publisher": f"P{i}"}
            for i in range(5)
        ],
    )
    assert news.chat_reply(result) == (
        "Headline\nOne. Two.\nP0: https://s0.example/a\nP1: https://s1.example/a\n"
        "P2: https://s2.example/a"
    )


async def test_rest_route(world):
    world["ddgs"] = [
        hit("https://n.example/a", "Mars rover finds water", "Water found.", "Space")
    ]
    agent = build_news_agent()
    route = agent._rest_handlers[("POST", "/news")]
    result = await route(None, NewsRequest(query="mars rover"))
    assert isinstance(result, NewsResponse)
    assert result.headline == "Mars rover finds water"
    assert result.sources[0].url == "https://n.example/a"
