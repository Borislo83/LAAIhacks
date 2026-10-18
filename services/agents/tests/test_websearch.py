"""General web search agent: no live network. Search, pages, LLM and weather are faked."""

import asyncio
from datetime import UTC, datetime, timedelta

import pytest

from laai_agents import llm, news, websearch
from laai_agents.agents import build_search_agent
from laai_agents.models import NewsResponse, SearchRequest, SearchResponse, Source
from laai_agents.search import SearchUnavailable

HOURS = (
    "Bruin Plate is open for breakfast from 7:00 a.m. to 10:00 a.m. on weekdays. "
    "Lunch is served from 11:00 a.m. to 3:00 p.m. The dining hall is in Carnesale Commons."
)


def page(title, body, site="UCLA Dining", image=None):
    image_meta = f'<meta property="og:image" content="{image}">' if image else ""
    return (
        f'<html><head><meta property="og:title" content="{title}">'
        f'<meta property="og:site_name" content="{site}">{image_meta}</head>'
        f"<body><article><p>{body}</p></article></body></html>"
    )


@pytest.fixture(autouse=True)
def offline(monkeypatch):
    monkeypatch.setattr(websearch, "_CACHE", type(websearch._CACHE)())
    monkeypatch.setattr(websearch, "_PLACES", {})
    monkeypatch.setattr(llm, "has_key", lambda: False)

    async def no_news(query, timelimit="w"):
        return []

    monkeypatch.setattr(news, "ddgs_news", no_news)

    async def no_page(url):
        return None

    monkeypatch.setattr(news, "fetch_page", no_page)

    async def no_weather(url, params, seconds):
        return None

    monkeypatch.setattr(websearch, "_get_json", no_weather)


def web(monkeypatch, sources, delay=0.0, calls=None):
    async def fake(query):
        if calls is not None:
            calls.append(query)
        await asyncio.sleep(delay)
        if isinstance(sources, Exception):
            raise sources
        return sources

    monkeypatch.setattr(websearch, "free_search", fake)


def pages(monkeypatch, by_url, delays=None):
    async def fake(url):
        await asyncio.sleep((delays or {}).get(url, 0))
        return by_url.get(url)

    monkeypatch.setattr(news, "fetch_page", fake)


DINING = "https://dining.ucla.edu/bruin-plate/"
YELP = "https://www.yelp.com/biz/bruin-plate"
OTHER = "https://example.com/unrelated"


def bruin_sources():
    return [
        Source(title="Bruin Plate | UCLA Dining", url=DINING, snippet="Hours and menus for Bruin Plate."),
        Source(title="Bruin Plate - Yelp", url=YELP, snippet="Healthy dining hall at UCLA..."),
        Source(title="Unrelated page", url=OTHER, snippet="Nothing about it."),
    ]


# --- Query and routing helpers ----------------------------------------------------------


@pytest.mark.parametrize(
    "text, query",
    [
        ("hey can you look up when Bruin Plate opens?", "when Bruin Plate opens"),
        ("search for best ramen near UCLA please", "best ramen near UCLA"),
        ("Google the population of Tokyo", "the population of Tokyo"),
        ("what is the Fetch.ai uAgents framework", "what is the Fetch.ai uAgents framework"),
    ],
)
def test_clean_query_strips_request_phrasing_but_keeps_the_question(text, query):
    assert websearch.clean_query(text) == query


def test_only_news_like_questions_count_recency():
    assert websearch.news_like("latest news on SpaceX")
    assert websearch.news_like("what happened at the debate")
    assert not websearch.news_like("when does Bruin Plate open")
    assert not websearch.news_like("weather in Los Angeles today")


@pytest.mark.parametrize(
    "text, place",
    [
        ("weather in LA", "Los Angeles"),
        ("weather in Los Angeles today", "Los Angeles"),
        ("what's the forecast for San Diego tomorrow?", "San Diego"),
        ("when does Bruin Plate open", None),
        ("weather", None),
    ],
)
def test_weather_place(text, place):
    assert websearch.weather_place(text) == place


# --- Grounding --------------------------------------------------------------------------


def test_a_sentence_with_a_number_not_in_the_sources_is_dropped():
    sources = "Bruin Plate opens at 7:00 a.m. on weekdays."
    answer = "Bruin Plate opens at 7:00 a.m. on weekdays. It seats 1,200 diners. It is popular."
    assert websearch.grounded(answer, sources) == "Bruin Plate opens at 7:00 a.m. on weekdays. It is popular."
    assert websearch.grounded("It seats 1,200 diners.", sources) is None
    # A number the user said is not invented.
    assert websearch.grounded("The 2026 schedule is out.", "The schedule is out.", "2026 schedule")


async def test_llm_answer_drops_unsupported_numbers(monkeypatch):
    monkeypatch.setattr(llm, "has_key", lambda: True)
    seen = {}

    async def complete(system, user, timeout=30.0):
        seen["user"], seen["timeout"] = user, timeout
        return "Bruin Plate opens at 7:00 a.m. on weekdays. Dinner ends at 21:30 every day."

    monkeypatch.setattr(llm, "complete", complete)
    web(monkeypatch, bruin_sources())
    pages(monkeypatch, {DINING: page("Bruin Plate", HOURS)})
    result = await websearch.answer(SearchRequest(query="when does Bruin Plate open"))
    assert result.summary == "Bruin Plate opens at 7:00 a.m. on weekdays."
    assert "Question: when does Bruin Plate open" in seen["user"] and HOURS[:40] in seen["user"]
    assert seen["timeout"] <= websearch.DEADLINE


async def test_llm_answer_with_only_invented_numbers_falls_back_to_the_pages(monkeypatch):
    monkeypatch.setattr(llm, "has_key", lambda: True)

    async def complete(system, user, timeout=30.0):
        return "It opens at 6:15 a.m."

    monkeypatch.setattr(llm, "complete", complete)
    web(monkeypatch, bruin_sources())
    pages(monkeypatch, {DINING: page("Bruin Plate", HOURS)})
    result = await websearch.answer(SearchRequest(query="when does Bruin Plate open"))
    assert "6:15" not in result.summary
    assert "7:00 a.m. to 10:00 a.m." in result.summary  # verbatim from the page


async def test_extractive_answer_without_a_key_quotes_matching_sentences(monkeypatch):
    web(monkeypatch, bruin_sources())
    pages(monkeypatch, {DINING: page("Bruin Plate", HOURS)})
    result = await websearch.answer(SearchRequest(query="when does Bruin Plate open"))
    assert result.summary.startswith("Bruin Plate is open for breakfast from 7:00 a.m. to 10:00 a.m.")
    assert news.numbers_supported(result.summary, HOURS + " Hours and menus for Bruin Plate.")


# --- Result shape -----------------------------------------------------------------------


async def test_result_has_the_news_shape_and_at_most_three_sources(monkeypatch):
    many = bruin_sources() + [
        Source(title=f"Bruin Plate guide {i}", url=f"https://guide{i}.example.com/bruin-plate",
               snippet="Bruin Plate hours.")
        for i in range(5)
    ]  # fmt: skip
    web(monkeypatch, many)
    pages(monkeypatch, {DINING: page("Bruin Plate", HOURS, image="https://img.example.com/plate.jpg")})
    result = await websearch.answer(SearchRequest(query="when does Bruin Plate open"))
    assert isinstance(result, SearchResponse)
    assert result.headline == result.title == "When does Bruin Plate open?"
    assert result.summary and result.error is None and not result.news
    assert 1 <= len(result.sources) <= 3
    assert result.sources[0].url == DINING and result.sources[0].publisher == "UCLA Dining"
    assert result.image_url == "https://img.example.com/plate.jpg"
    # Every field the lens and the orchestrator's news sanitizer read is present.
    data = result.dict()
    for key in ("headline", "summary", "sources", "image_url", "image_alt", "image_source_url", "error"):
        assert key in data
    # The news model accepts it too: same field types.
    NewsResponse(**{k: data[k] for k in NewsResponse.__fields__ if k in data})


async def test_unrelated_results_rank_below_matching_ones(monkeypatch):
    sources = list(reversed(bruin_sources()))  # the unrelated page comes first
    web(monkeypatch, sources)
    result = await websearch.answer(SearchRequest(query="when does Bruin Plate open"))
    assert result.sources[-1].url == OTHER


async def test_news_like_questions_use_news_search_and_recency(monkeypatch):
    now = datetime.now(UTC)
    old = (now - timedelta(days=30)).isoformat()
    new = (now - timedelta(hours=2)).isoformat()

    async def fake_news(query, timelimit="w"):
        return [
            news._hit("https://news.example.com/old", "Starship test flight report", "Starship flew.", "A", old),
            news._hit("https://news.example.com/new", "Starship launches again", "Starship launched today.", "B", new),
        ]

    monkeypatch.setattr(news, "ddgs_news", fake_news)
    web(monkeypatch, [])
    result = await websearch.answer(SearchRequest(query="latest news on Starship"))
    assert result.news and result.data_source == "web+news"
    assert result.headline == "Starship launches again"


async def test_cache_answers_a_repeat_without_searching(monkeypatch):
    calls = []
    web(monkeypatch, bruin_sources(), calls=calls)
    first = await websearch.answer(SearchRequest(query="when does Bruin Plate open"))
    second = await websearch.answer(SearchRequest(query="When does Bruin Plate open?"))
    assert len(calls) == 1 and second.summary == first.summary


# --- Deadline and failures --------------------------------------------------------------


async def test_slow_pages_are_cut_at_the_deadline_and_snippets_answer(monkeypatch):
    monkeypatch.setattr(websearch, "DEADLINE", 1.0)
    web(monkeypatch, bruin_sources())
    pages(monkeypatch, {DINING: page("Bruin Plate", HOURS)}, delays={DINING: 5, YELP: 5, OTHER: 5})
    started = asyncio.get_running_loop().time()
    result = await websearch.answer(SearchRequest(query="when does Bruin Plate open"))
    assert asyncio.get_running_loop().time() - started < 1.3
    assert result.partial and result.error is None
    assert result.summary == "Hours and menus for Bruin Plate."  # the snippet, verbatim
    assert result.elapsed_ms < 1300
    # A partial answer is cached only briefly.
    [(_, ttl, _)] = websearch._CACHE.values()
    assert ttl == websearch.PARTIAL_CACHE_SECONDS


async def test_slow_search_still_answers_within_the_deadline(monkeypatch):
    monkeypatch.setattr(websearch, "DEADLINE", 2.0)
    web(monkeypatch, bruin_sources(), delay=1.4)  # past the search share, inside the grace
    started = asyncio.get_running_loop().time()
    result = await websearch.answer(SearchRequest(query="when does Bruin Plate open"))
    assert asyncio.get_running_loop().time() - started < 2.2
    assert result.error is None and result.sources and result.summary


async def test_search_that_never_answers_times_out_honestly(monkeypatch):
    monkeypatch.setattr(websearch, "DEADLINE", 1.0)
    web(monkeypatch, bruin_sources(), delay=10)
    started = asyncio.get_running_loop().time()
    result = await websearch.answer(SearchRequest(query="what is uAgents"))
    assert asyncio.get_running_loop().time() - started < 1.3
    assert result.error == "Search timed out" and not result.sources and "nothing was made up" in result.summary


async def test_spare_engines_answer_when_the_main_search_is_empty(monkeypatch):
    web(monkeypatch, SearchUnavailable("empty"))

    def rows(query, backend):
        if backend != "wikipedia":
            raise RuntimeError("rate limited")
        return [{"href": "https://en.wikipedia.org/wiki/UAgents", "title": "uAgents",
                 "body": "uAgents is a Python framework for building autonomous agents."}]  # fmt: skip

    monkeypatch.setattr(websearch, "_spare_rows", rows)
    result = await websearch.answer(SearchRequest(query="what is uAgents"))
    assert result.error is None and result.sources[0].url == "https://en.wikipedia.org/wiki/UAgents"
    assert result.summary == "uAgents is a Python framework for building autonomous agents."


async def test_search_unavailable_is_an_error_not_a_guess(monkeypatch):
    web(monkeypatch, SearchUnavailable("down"))

    def none(query, backend):
        raise RuntimeError("down too")

    monkeypatch.setattr(websearch, "_spare_rows", none)
    result = await websearch.answer(SearchRequest(query="what is uAgents"))
    assert result.error == "Search unavailable" and not result.sources and not result.headline


async def test_no_results(monkeypatch):
    web(monkeypatch, [])
    result = await websearch.answer(SearchRequest(query="what is xyzzyplugh"))
    assert result.error is None and result.summary == "No web results found for what is xyzzyplugh."


async def test_llm_that_runs_past_the_deadline_is_dropped(monkeypatch):
    monkeypatch.setattr(websearch, "DEADLINE", 2.5)
    monkeypatch.setattr(llm, "has_key", lambda: True)

    async def slow(system, user, timeout=30.0):
        await asyncio.sleep(10)
        return "Never."

    monkeypatch.setattr(llm, "complete", slow)
    web(monkeypatch, bruin_sources())
    pages(monkeypatch, {DINING: page("Bruin Plate", HOURS)})
    started = asyncio.get_running_loop().time()
    result = await websearch.answer(SearchRequest(query="when does Bruin Plate open"))
    assert asyncio.get_running_loop().time() - started < 2.7
    assert "7:00 a.m." in result.summary and result.partial


# --- Weather ----------------------------------------------------------------------------


async def test_weather_comes_from_the_forecast_numbers(monkeypatch):
    requested = []

    async def fake(url, params, seconds):
        requested.append(url)
        if url == websearch.GEOCODE:
            assert params["name"] == "Los Angeles"
            return {"results": [{"name": "Los Angeles", "admin1": "California",
                                 "latitude": 34.05, "longitude": -118.24}]}  # fmt: skip
        return {
            "current": {"temperature_2m": 74.6, "apparent_temperature": 72.1, "weather_code": 0,
                        "wind_speed_10m": 6.2},
            "daily": {"temperature_2m_max": [88.0, 85.0], "temperature_2m_min": [63.4, 61.0],
                      "precipitation_probability_max": [3, 0], "weather_code": [0, 1]},
        }  # fmt: skip

    monkeypatch.setattr(websearch, "_get_json", fake)
    web(monkeypatch, bruin_sources())
    result = await websearch.answer(SearchRequest(query="weather in LA today"))
    assert requested == [websearch.GEOCODE, websearch.FORECAST]
    assert result.data_source == "open-meteo" and result.headline == "Weather in Los Angeles"
    assert result.summary == (
        "Los Angeles, California: 75°F and clear now, feels like 72°F, wind 6 mph. "
        "Today: clear, high 88°F, low 63°F, 3% chance of rain."
    )
    assert result.sources[0].url == "https://open-meteo.com/" and len(result.sources) <= 3


async def test_weather_failure_falls_back_to_web_search(monkeypatch):
    web(monkeypatch, bruin_sources())
    result = await websearch.answer(SearchRequest(query="weather in Westwood"))
    assert result.data_source == "web" and result.sources[0].url == DINING


# --- Agent wiring -----------------------------------------------------------------------


async def test_search_agent_serves_post_search(monkeypatch):
    web(monkeypatch, bruin_sources())
    agent = build_search_agent()
    assert agent.name == "laai-search"
    route = agent._rest_handlers[("POST", "/search")]
    result = await route(None, SearchRequest(query="when does Bruin Plate open"))
    assert isinstance(result, SearchResponse) and result.sources[0].url == DINING


def test_chat_reply_lists_answer_and_sources():
    result = SearchResponse(query="q", headline="Q?", summary="A.", sources=[
        news.NewsSource(title="t", url=DINING, publisher="UCLA Dining")])  # fmt: skip
    assert websearch.chat_reply(result) == f"Q?\nA.\nUCLA Dining: {DINING}"


def test_a_temperature_relabeled_to_another_unit_is_dropped():
    sources = "Los Angeles forecast: high 30 °C (86 °F) today; average 26.6°C for October."
    bad = "The forecast high is 30 °F. Average is 26.6 °F."
    assert websearch.grounded(bad, sources) is None
    good = "The forecast high is 30 °C (86 °F)."
    assert websearch.grounded(good, sources) == good
    assert websearch.grounded("Winds reach 9 mph.", "wind 9 mph") == "Winds reach 9 mph."


def test_wttr_backup_reads_now_and_tomorrow():
    from laai_agents.websearch import wttr_summary

    data = {"nearest_area": [{"areaName": [{"value": "Los Angeles"}], "region": [{"value": "California"}]}],
            "current_condition": [{"temp_F": "79", "weatherDesc": [{"value": "Sunny"}]}],
            "weather": [{"maxtempF": "86", "mintempF": "75"}, {"maxtempF": "83", "mintempF": "72"}]}
    assert wttr_summary("LA", data, False) == "Los Angeles, California: 79°F and sunny now. Today: high 86°F, low 75°F."
    assert wttr_summary("LA", data, True) == "Los Angeles, California, tomorrow: high 83°F, low 72°F."
    assert wttr_summary("LA", {}, False) is None
