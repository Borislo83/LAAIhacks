"""Fixes from the backend audit: honest watcher results, bounded latency, no leaks."""

import asyncio
import time

from laai_agents import cart_action, live, search as search_module
from laai_agents.models import AlternativesRequest, PriceCompareRequest, Source

ASIN = "B0FQFB8FMG"


# --- (b) The cart proposal names the seller once -----------------------------------------


def test_seller_shown_twice_on_the_page_is_named_once():
    # Amazon's page carries the merchant line twice (desktop and mobile layouts); the
    # proposal read "sold by Amazon.com Amazon.com".
    page = f"""<html><body><input type="hidden" id="ASIN" value="{ASIN}">
      <input id="add-to-cart-button" type="submit">
      <span id="productTitle">Apple AirPods Pro 3 Wireless Earbuds</span>
      <div id="corePriceDisplay_desktop_feature_div"><span class="a-price">
        <span class="a-offscreen">$199.00</span></span></div>
      <div id="merchantInfoFeature_feature_div">
        <div><span class="a-size-small offer-display-feature-text-message">Amazon.com</span></div>
        <div><span class="a-size-small offer-display-feature-text-message">Amazon.com</span></div>
      </div></body></html>"""
    read = cart_action.parse_listing_page(page, ASIN)
    assert read["seller"] == "Amazon.com"
    listing = {**read, "asin": ASIN}
    assert "sold by Amazon.com." in cart_action.describe(listing)


# --- (a) Alternatives: no older generations as rivals, no near-duplicates ----------------

VS_TITLES = [
    "AirPods Pro 3 vs AirPods Pro 2: should you upgrade?",
    "AirPods Pro 2 vs AirPods Pro 3 compared",
    "AirPods Pro 3 vs AirPods 5",
    "AirPods 5 vs AirPods Pro 3",
    "Apple AirPods Pro 1 vs AirPods Pro 3",
    "AirPods Pro 3 vs AirPods 5 earbuds",
]


def _sources(titles):
    return [Source(title=t, url=f"https://reviews.example/{i}") for i, t in enumerate(titles)]


def test_near_duplicate_rival_names_collapse():
    rivals = [name for name, _ in live.compared_products(_sources(VS_TITLES), "AirPods Pro 3")]
    assert "AirPods 5 earbuds" not in rivals and rivals.count("AirPods 5") == 1
    assert sum("Pro 1" in r for r in rivals) == 1
    counts = dict(live.compared_products(_sources(VS_TITLES), "AirPods Pro 3"))
    assert counts["AirPods 5"] == 3  # the merged names' comparisons add up


async def test_older_generations_are_not_offered_as_alternatives(monkeypatch):
    async def search(query):
        sources = _sources(VS_TITLES) if query.endswith(" vs") else []
        return sources, {"sources": sources, "data_source": "ddgs", "notice": None}

    async def compare(req, deadline=None):
        return type("R", (), {"best": None, "sources": []})()

    async def no_store_alternatives(*args):
        return []

    monkeypatch.setattr(live, "search", search)
    monkeypatch.setattr(live, "compare", compare)
    monkeypatch.setattr(live, "store_alternatives", no_store_alternatives)
    result = await live.alternatives(AlternativesRequest(product="AirPods Pro 3"))
    first_sentence = result.summary.split(". ")[0]
    assert first_sentence == "Alternatives to consider: AirPods 5"
    # The product's own other generations are named only as such, after the rivals.
    assert "older model" in result.summary and "AirPods Pro 2" in result.summary
    names = [a.product for a in result.alternatives]
    assert names[0] == "AirPods 5"
    assert all(live.other_generation(n, "AirPods Pro 3") for n in names[1:])
    # Plain wording: no "Stores didn't share prices with the search".
    assert "share prices" not in result.summary
    assert "compare prices yourself" in result.summary


async def test_older_generation_with_a_price_is_never_the_top_pick(monkeypatch):
    from laai_agents.models import Offer

    async def search(query):
        sources = _sources(VS_TITLES[:4]) if query.endswith(" vs") else []
        return sources, {"sources": sources, "data_source": "ddgs", "notice": None}

    async def compare(req, deadline=None):
        best = Offer(product=req.product, store="amazon.com", price=169.0,
                     url="https://www.amazon.com/dp/B0D1XD1ZV3", verified=True, trust="high")
        if "Pro 2" not in req.product:
            best = None
        return type("R", (), {"best": best, "sources": []})()

    async def no_store_alternatives(*args):
        return []

    monkeypatch.setattr(live, "search", search)
    monkeypatch.setattr(live, "compare", compare)
    monkeypatch.setattr(live, "store_alternatives", no_store_alternatives)
    result = await live.alternatives(AlternativesRequest(product="AirPods Pro 3"))
    assert "Top pick" not in result.summary
    assert result.summary.startswith("Alternatives to consider: AirPods 5.")
    assert "AirPods Pro 2 (older model, $169.00 at Amazon)" in result.summary


async def test_a_timed_out_alternatives_search_says_so(monkeypatch):
    async def slow_search(query):
        await asyncio.sleep(10)

    async def no_store_alternatives(*args):
        return []

    monkeypatch.setattr(live, "search", slow_search)
    monkeypatch.setattr(live, "store_alternatives", no_store_alternatives)
    monkeypatch.setattr(live, "ALT_DEADLINE", 0.2)
    result = await live.alternatives(AlternativesRequest(product="AirPods Pro 3"))
    assert result.partial
    # Not "Found alternative research sources ... no known products" with no sources.
    assert "research sources" not in result.summary
    assert "in time" in result.summary
    assert "unavailable" not in (result.notice or "")


# --- (c) Latency: a slow ASI search no longer holds every search -------------------------


async def test_auto_search_does_not_wait_for_a_slow_asi_answer(monkeypatch):
    cancelled = asyncio.Event()

    async def slow_asi(query):
        try:
            await asyncio.sleep(8)  # ASI measured at 8.2 s with no usable citations
        except asyncio.CancelledError:
            cancelled.set()
            raise
        return [Source(title="late", url="https://late.example")]

    async def free(query):
        return [Source(title="a", url="https://a.example")]

    monkeypatch.setenv("SEARCH_PROVIDER", "auto")
    monkeypatch.setattr(search_module, "asi_search", slow_asi)
    monkeypatch.setattr(search_module, "free_search", free)
    monkeypatch.setattr(search_module, "ASI_GRACE", 0.1)
    started = time.monotonic()
    sources, meta = await search_module.search("q")
    assert time.monotonic() - started < 1
    assert [s.url for s in sources] == ["https://a.example"]
    assert meta["data_source"] == "ddgs"
    await asyncio.sleep(0)
    assert cancelled.is_set()  # no orphaned ASI request


async def test_auto_search_still_waits_for_asi_when_free_search_fails(monkeypatch):
    async def asi(query):
        await asyncio.sleep(0.3)
        return [Source(title="b", url="https://b.example")]

    async def down(query):
        raise search_module.SearchUnavailable("down")

    monkeypatch.setenv("SEARCH_PROVIDER", "auto")
    monkeypatch.setattr(search_module, "asi_search", asi)
    monkeypatch.setattr(search_module, "free_search", down)
    monkeypatch.setattr(search_module, "ASI_GRACE", 0.05)
    sources, meta = await search_module.search("q")
    assert meta["data_source"] == "asi_web_search" and sources[0].url == "https://b.example"


async def test_cancelling_a_search_cancels_both_providers(monkeypatch):
    stopped = []

    async def hang(name):
        try:
            await asyncio.sleep(10)
        except asyncio.CancelledError:
            stopped.append(name)
            raise

    monkeypatch.setenv("SEARCH_PROVIDER", "auto")
    monkeypatch.setattr(search_module, "asi_search", lambda q: hang("asi"))
    monkeypatch.setattr(search_module, "free_search", lambda q: hang("ddgs"))
    task = asyncio.create_task(search_module.search("q"))
    await asyncio.sleep(0.05)
    task.cancel()
    try:
        await task
    except asyncio.CancelledError:
        pass
    await asyncio.sleep(0)
    assert sorted(stopped) == ["asi", "ddgs"]


async def test_a_rival_price_lookup_answers_by_the_time_left(monkeypatch):
    """compared_alternatives passes its remaining time to each rival's price lookup, so a
    rival's store prices found in time are kept instead of being cancelled at the deadline."""
    seen = []

    async def search(query):
        sources = _sources(["AirPods Pro 3 vs AirPods 5", "AirPods 5 vs AirPods Pro 3"])
        return sources, {"sources": sources, "data_source": "ddgs", "notice": None}

    async def compare(req, deadline=None):
        seen.append(deadline)
        return type("R", (), {"best": None, "sources": [], "partial": True})()

    monkeypatch.setattr(live, "search", search)
    monkeypatch.setattr(live, "compare", compare)
    until = time.monotonic() + 2.0
    priced, unpriced = await live.compared_alternatives("AirPods Pro 3", None, until)
    assert seen and all(d is not None and d < 2.0 for d in seen)
    assert "in time" in unpriced[0].reason


async def test_a_failed_asi_search_left_behind_is_not_logged_as_unretrieved(monkeypatch):
    errors = []
    loop = asyncio.get_running_loop()
    loop.set_exception_handler(lambda loop, context: errors.append(context["message"]))

    async def asi(query):
        raise search_module.SearchUnavailable("ASI:One API key is not configured")

    async def slow_free(query):
        await asyncio.sleep(10)

    monkeypatch.setenv("SEARCH_PROVIDER", "auto")
    monkeypatch.setattr(search_module, "asi_search", asi)
    monkeypatch.setattr(search_module, "free_search", slow_free)
    # A caller's deadline cancels the search while ASI has already failed.
    try:
        await asyncio.wait_for(search_module.search("q"), 0.05)
    except TimeoutError:
        pass
    import gc

    gc.collect()
    await asyncio.sleep(0)
    loop.set_exception_handler(None)
    assert not [e for e in errors if "never retrieved" in e]


# --- Robustness: the watcher CLI closes the shared HTTP session it used -------------------


async def test_watcher_cli_closes_the_shared_http_session(monkeypatch, tmp_path):
    import argparse

    from laai_agents import integration, net

    opened = []

    async def one_pass(self):
        opened.append(net.session())  # research inside the pass uses the shared session
        return 0

    monkeypatch.setenv("GLASSES_API_URL", "http://localhost:1")
    monkeypatch.setattr(integration.PageWatcher, "watch_once", one_pass)
    args = argparse.Namespace(command="watch", once=True, journal=str(tmp_path / "w.json"),
                              interval=2.0)
    await integration.run_cli(args)
    assert opened and opened[0].closed


# --- Robustness: one failed half doesn't sink the watcher's compare_prices ---------------


async def test_compare_prices_action_keeps_prices_when_alternatives_fail(monkeypatch, tmp_path):
    from laai_agents import handlers, integration
    from laai_agents.models import AlternativesResponse, PriceCompareResponse

    async def price(req):
        return PriceCompareResponse(product=req.product, offers=[], summary="Best $199 at Amazon.")

    async def alternatives_down(req):
        return AlternativesResponse(product=req.product, alternatives=[],
                                    error="Live alternative search unavailable", summary="x")

    async def alternatives_crash(req):
        raise RuntimeError("bug")

    monkeypatch.setattr(handlers, "compare_prices", price)
    workflow = integration.GlassesWorkflow("http://localhost:1", {}, tmp_path / "j.json",
                                           state={"cursor": "", "actions": {}, "outcomes": {}})
    plan = {"handler": "compare_prices", "product": "AirPods Pro 3", "expires_at": "x"}
    for broken in (alternatives_down, alternatives_crash):
        monkeypatch.setattr(handlers, "find_alternatives", broken)
        outcome = await workflow.run_plan(plan)
        assert outcome["status"] == "completed", broken
        assert outcome["message"].startswith("Best $199 at Amazon.")
        assert "Alternatives couldn't be checked right now." in outcome["message"]

    async def price_down(req):
        raise RuntimeError("down")

    monkeypatch.setattr(handlers, "compare_prices", price_down)
    assert (await workflow.run_plan(plan))["status"] == "failed"  # nothing to show


async def test_a_price_lookup_cut_short_is_not_cached_for_full_lookups(monkeypatch):
    from laai_agents import merchants, stores
    from laai_agents.models import Offer

    calls = []

    async def slow_search(query):
        calls.append(query)
        await asyncio.sleep(10)

    async def listings(name, budget=None):
        return [Offer(product="AirPods 5", store="amazon.com", price=129.0,
                      url="https://www.amazon.com/dp/B0HJB76H2V", verified=True, trust="high")]

    monkeypatch.setattr(live, "search", slow_search)
    monkeypatch.setattr(live, "store_listings", listings)
    short = await live.compare(PriceCompareRequest(product="AirPods 5"), deadline=0.05)
    assert short.partial and short.best.price == 129.0  # what arrived in time is kept
    monkeypatch.setattr(live, "PRICE_DEADLINE", 0.1)
    await live.compare(PriceCompareRequest(product="AirPods 5"))
    assert len(calls) == 2  # the full lookup ran again instead of reusing the short one


async def test_llm_reply_without_text_is_no_reply_not_a_crash(monkeypatch):
    from laai_agents import llm, net

    class Response:
        def __init__(self, data):
            self.data = data

        async def __aenter__(self):
            return self

        async def __aexit__(self, *exc):
            return False

        def raise_for_status(self):
            pass

        async def json(self):
            if isinstance(self.data, Exception):
                raise self.data
            return self.data

    class Session:
        def __init__(self, data):
            self.data = data

        def post(self, *args, **kwargs):
            return Response(self.data)

    monkeypatch.setattr(llm.asi, "api_key", lambda: "test-key")
    for data in ({"choices": [{"message": {"content": None}}]}, ["not", "a", "dict"],
                 ValueError("bad json")):
        monkeypatch.setattr(net, "session", lambda d=data: Session(d))
        assert await llm.complete("system", "user") is None


async def test_malformed_asi_search_answer_falls_back_to_free_search(monkeypatch):
    from laai_agents import net

    class Response:
        status = 200

        async def __aenter__(self):
            return self

        async def __aexit__(self, *exc):
            return False

        async def json(self):
            return {"output": ["not an object", None]}

    class Session:
        def post(self, *args, **kwargs):
            return Response()

    async def free(query):
        return [Source(title="a", url="https://a.example")]

    monkeypatch.setenv("SEARCH_PROVIDER", "auto")
    monkeypatch.setattr(search_module.asi, "api_key", lambda: "test-key")
    monkeypatch.setattr(net, "session", lambda: Session())
    monkeypatch.setattr(search_module, "free_search", free)
    sources, meta = await search_module.search("q")
    assert meta["data_source"] == "ddgs" and sources[0].url == "https://a.example"


async def test_real_rivals_are_priced_before_other_generations(monkeypatch):
    order = []

    async def search(query):
        # The older model is the most compared one here.
        sources = _sources(VS_TITLES + ["AirPods Pro 2 vs AirPods Pro 3"] * 3)
        return sources, {"sources": sources, "data_source": "ddgs", "notice": None}

    async def compare(req, deadline=None):
        order.append(req.product)
        return type("R", (), {"best": None, "sources": []})()

    monkeypatch.setattr(live, "search", search)
    monkeypatch.setattr(live, "compare", compare)
    await live.compared_alternatives("AirPods Pro 3", None)
    assert order[0] == "AirPods 5"


async def test_a_failed_research_action_is_logged_with_its_cause(monkeypatch, tmp_path, caplog):
    from laai_agents import handlers, integration

    async def broken(req):
        raise KeyError("parser bug")

    monkeypatch.setattr(handlers, "get_reviews", broken)
    workflow = integration.GlassesWorkflow("http://localhost:1", {}, tmp_path / "j.json",
                                           state={"cursor": "", "actions": {}, "outcomes": {}})
    outcome = await workflow.run_plan({"handler": "find_reviews", "product": "AirPods Pro 3"})
    assert outcome == {"status": "failed", "message": "Live research unavailable"}
    assert any(r.exc_info and "parser bug" in str(r.exc_info[1]) for r in caplog.records)


def test_an_unverified_older_model_price_says_so():
    from laai_agents.models import Alternative

    lead = Alternative(product="AirPods Pro 2", price=249.31, store="us.smartprix.com",
                       verified=False, url="https://x.example", reason="r")
    assert live._same_line_label(lead, "AirPods Pro 3") == (
        "AirPods Pro 2 (older model, $249.31 at us.smartprix.com, unverified)"
    )
