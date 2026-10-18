import asyncio
from collections import OrderedDict

from laai_agents import live, net
from laai_agents import search as search_module
from laai_agents.models import AlternativesRequest, PriceCompareResponse, Source


async def test_shared_session_is_reused_and_closed():
    first = net.session()
    assert net.session() is first
    await net.close()
    assert first.closed and net.session() is not first
    await net.close()


async def test_auto_search_runs_providers_concurrently(monkeypatch):
    started = []
    both = asyncio.Event()

    async def provider(name, sources):
        started.append(name)
        if len(started) == 2:
            both.set()
        await asyncio.wait_for(both.wait(), 1)  # deadlocks if run one after the other
        return sources

    shared = Source(title="a", url="https://a.example/1")
    monkeypatch.setenv("SEARCH_PROVIDER", "auto")
    monkeypatch.setattr(
        search_module,
        "asi_search",
        lambda q: provider("asi", [shared, Source(title="b", url="https://b.example")]),
    )
    monkeypatch.setattr(search_module, "free_search", lambda q: provider("ddgs", [shared]))
    sources, meta = await search_module.search("q")
    assert [s.url for s in sources] == ["https://a.example/1", "https://b.example"]
    assert meta["data_source"] == "asi_web_search+ddgs"


async def test_auto_search_falls_back_and_keeps_asi_when_snippets_fail(monkeypatch):
    async def down(q):
        raise search_module.SearchUnavailable("down")

    async def found(q):
        return [Source(title="a", url="https://a.example")]

    monkeypatch.setenv("SEARCH_PROVIDER", "auto")
    monkeypatch.setattr(search_module, "asi_search", down)
    monkeypatch.setattr(search_module, "free_search", found)
    _, meta = await search_module.search("q")
    assert meta["data_source"] == "ddgs" and "fallback" in meta["notice"]
    monkeypatch.setattr(search_module, "asi_search", found)
    monkeypatch.setattr(search_module, "free_search", down)
    _, meta = await search_module.search("q")
    assert meta["data_source"] == "asi_web_search"


def test_search_cache_is_bounded_and_expires(monkeypatch):
    now = [0.0]
    monkeypatch.setattr(search_module.time, "monotonic", lambda: now[0])
    monkeypatch.setattr(search_module, "_CACHE", OrderedDict())
    monkeypatch.setattr(search_module, "CACHE_SIZE", 2)
    for query in ("a", "b", "c"):
        search_module._remember(query, [])
    assert list(search_module._CACHE) == ["b", "c"]
    now[0] = search_module.CACHE_SECONDS
    assert search_module._cached("c") is None and not search_module._CACHE


async def test_alternatives_search_runs_alongside_candidate_compares(monkeypatch):
    started = []
    all_started = asyncio.Event()

    async def wait_for_all(name):
        started.append(name)
        if len(started) == 3:  # one category search + two same-category candidates
            all_started.set()
        await asyncio.wait_for(all_started.wait(), 1)

    async def search(query):
        await wait_for_all("search")
        return [], {"sources": [], "data_source": "ddgs", "notice": "n"}

    async def compare(req):
        await wait_for_all(req.product)
        return PriceCompareResponse(product=req.product, offers=[], summary="none")

    monkeypatch.setattr(live, "search", search)
    monkeypatch.setattr(live, "compare", compare)
    result = await live.alternatives(AlternativesRequest(product="Sony XM5"))
    assert len(result.alternatives) == 2


def test_product_names_are_parsed_once():
    live._name_words.cache_clear()
    for _ in range(5):
        assert live.matches_product("Sony WH-1000XM5 headphones", "Sony XM5")
    assert live._name_words.cache_info().misses == 1
