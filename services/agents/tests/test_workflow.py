from datetime import UTC, datetime

import pytest
from laai_agents import coordinator, handlers, live
from laai_agents.integration import GlassesWorkflow
from laai_agents.models import (
    AlternativesResponse,
    ConsiderRequest,
    PriceCompareResponse,
    Source,
)


async def test_coordinator_delegates_all_specialists_and_budget():
    class Context:
        def __init__(self):
            self.requests = []

        async def send_and_receive(self, destination, message, response_type, timeout):
            self.requests.append((destination, message.model_dump()))
            response = response_type(
                product="Sony WH-1000XM5",
                summary=destination,
                **(
                    {"offers": []}
                    if response_type is PriceCompareResponse
                    else {"alternatives": []}
                    if response_type is AlternativesResponse
                    else {}
                ),
            )
            return response, None

    ctx = Context()
    request = await coordinator.parse_request(
        "Compare Sony XM5 prices, check reviews, and recommend an alternative under $300."
    )
    result = await coordinator.consider(
        ctx,
        {
            "laai-price-compare": "price",
            "laai-reviews": "reviews",
            "laai-alternatives": "alternatives",
        },
        request,
    )
    assert request.product == "Sony WH-1000XM5" and request.max_price == 300
    assert {destination for destination, _ in ctx.requests} == {
        "price",
        "reviews",
        "alternatives",
    }
    assert dict(ctx.requests)["alternatives"]["max_price"] == 300
    assert "DEMO" in result.summary
    assert not result.errors
    assert await coordinator.parse_request("consider this product") is None


async def test_coordinator_retries_failed_specialists_three_times(monkeypatch):
    monkeypatch.setattr(coordinator, "RETRY_DELAY", 0)
    tries = {}

    class Context:
        async def send_and_receive(self, destination, message, response_type, timeout):
            tries[destination] = tries.get(destination, 0) + 1
            if destination == "price":
                raise TimeoutError  # never answers
            extra = {"alternatives": []} if response_type is AlternativesResponse else {}
            error = "search down" if tries[destination] < 3 else None
            return response_type(product="x", summary="s", error=error, **extra), None

    result = await coordinator.consider(
        Context(),
        {"laai-price-compare": "price", "laai-reviews": "reviews", "laai-alternatives": "alts"},
        ConsiderRequest(product="Sony WH-1000XM5"),
    )
    assert tries == {"price": 3, "reviews": 3, "alts": 3}
    assert result.reviews.error is None and result.alternatives.error is None
    assert result.price is None and "price" in result.errors


async def test_coordinator_compares_reviews_with_top_alternative():
    from laai_agents.models import Alternative, ReviewsResponse

    class Context:
        def __init__(self):
            self.reviewed = []

        async def send_and_receive(self, destination, message, response_type, timeout):
            if response_type is AlternativesResponse:
                return response_type(
                    product="Sony WH-1000XM5",
                    summary="alts",
                    alternatives=[
                        Alternative(product="Bose QuietComfort Ultra", url="https://x.example", reason="r")
                    ],
                ), None
            if response_type is ReviewsResponse:
                self.reviewed.append(message.product)
                return response_type(product=message.product, summary="r"), None
            return response_type(product=message.product, summary="p", offers=[]), None

    ctx = Context()
    result = await coordinator.consider(
        ctx,
        {"laai-price-compare": "p", "laai-reviews": "r", "laai-alternatives": "a"},
        ConsiderRequest(product="Sony WH-1000XM5", max_price=300),
    )
    assert sorted(ctx.reviewed) == ["Bose QuietComfort Ultra", "Sony WH-1000XM5"]
    assert result.alternative_reviews.product == "Bose QuietComfort Ultra"
    assert "Reviews compared" in result.summary


async def test_alternatives_keep_candidates_when_stores_hide_prices(monkeypatch):
    async def search(query):
        return [], {"sources": [], "data_source": "ddgs", "notice": "n"}

    async def no_price(req):
        return PriceCompareResponse(product=req.product, offers=[], summary="none")

    monkeypatch.setattr(live, "search", search)
    monkeypatch.setattr(live, "compare", no_price)
    from laai_agents.models import AlternativesRequest

    result = await live.alternatives(AlternativesRequest(product="Sony XM5", max_price=300))
    assert result.alternatives and all(a.price is None for a in result.alternatives)
    assert "Alternatives to consider" in result.summary


async def test_free_search_retries_other_backends_and_caches(monkeypatch):
    import ddgs

    from laai_agents import search as search_module

    calls = []

    class FakeDDGS:
        def __init__(self, timeout):
            pass

        def text(self, query, region, max_results, backend):
            calls.append(backend)
            if backend == search_module.BACKENDS[0]:
                raise ddgs.exceptions.DDGSException("rate limited")
            return [{"title": "t", "href": "https://a.example/p", "body": "b"}]

    monkeypatch.setattr(ddgs, "DDGS", FakeDDGS)
    monkeypatch.setattr(search_module, "_CACHE", {})
    first = await search_module.free_search("cache me")
    second = await search_module.free_search("cache me")
    assert first == second and calls == list(search_module.BACKENDS[:2])


async def test_live_prices_keep_snippet_leads_but_reject_variants_and_ambiguity(
    monkeypatch,
):
    from laai_agents import merchants

    async def search(query):
        sources = [
            Source(
                title="Sony WH-1000XM4",
                url="https://shop.example/xm4",
                snippet="$99.00",
            ),
            Source(
                title="Sony WH-1000XM5",
                url="https://shop.example/installment",
                snippet="$10.00/mo",
            ),
            Source(
                title="Sony WH-1000XM5",
                url="https://shop.example/range",
                snippet="$249.00 to $299.00",
            ),
            Source(
                title="Sony WH-1000XM5",
                url="https://shop.example/xm5",
                snippet="Price $279.00",
            ),
            Source(
                title="Sony WH-1000XM5 Wireless Headphones",
                url="https://www.bestbuy.com/site/xm5",
                snippet="Sale $299.99",
            ),
        ]
        return sources, {
            "sources": sources,
            "data_source": "ddgs",
            "notice": "Search lead",
        }

    async def no_pages(url, product):
        return []

    monkeypatch.setattr(live, "search", search)
    monkeypatch.setattr(merchants, "fetch_offers", no_pages)
    result = await live.compare(type("Request", (), {"product": "Sony XM5"})())
    # Single clear snippet prices are kept as leads; XM4, /mo and ranges are not.
    assert [(o.store, o.price, o.trust) for o in result.offers] == [
        ("bestbuy.com", 299.99, "medium"),
        ("shop.example", 279.0, "low"),
    ]
    # A cheaper unknown-store lead doesn't outrank a trusted store.
    assert result.best.store == "bestbuy.com"
    assert result.data_source == "ddgs"


def test_merchant_offers_are_broad_but_drop_clear_mismatches():
    import json

    from laai_agents.merchants import fetchable, parse_offers, rank

    product = {
        "@type": "Product",
        "name": "Sony WH-1000XM5",
        "offers": {
            "@type": "Offer",
            "price": "279.99",
            "priceCurrency": "USD",
            "availability": "https://schema.org/InStock",
        },
    }

    def page(node):
        return '<script type="application/ld+json">' + json.dumps(node) + "</script>"

    [offer] = parse_offers(
        page(product), "https://www.bestbuy.com/product/test", "Sony XM5"
    )
    assert (offer.price, offer.trust, offer.source) == (279.99, "high", "structured")
    # Any store is considered; lookalike domains just don't get trusted-store credit.
    [other] = parse_offers(
        page(product), "https://www.bestbuy.com.evil.example/p", "Sony XM5"
    )
    assert other.trust == "medium"
    # Aggregate offers and offers without an explicit currency still count.
    product["offers"] = {"@type": "AggregateOffer", "lowPrice": "259.00"}
    assert (
        parse_offers(page(product), "https://www.target.com/p/x", "Sony XM5")[0].price
        == 259.0
    )
    product["offers"] = {"@type": "Offer", "price": "279.99", "priceCurrency": "USD"}
    product["name"] = "Sony WH-1000XM4"
    assert not parse_offers(
        page(product), "https://www.bestbuy.com/product/test", "Sony XM5"
    )
    product["name"] = "Sony WH-1000XM5"
    for key, value in [
        ("priceCurrency", "CAD"),
        ("availability", "https://schema.org/OutOfStock"),
        ("itemCondition", "https://schema.org/UsedCondition"),
    ]:
        bad = dict(product, offers=dict(product["offers"], **{key: value}))
        assert not parse_offers(page(bad), "https://www.bestbuy.com/p", "Sony XM5")
    meta = (
        '<meta property="og:title" content="Sony WH-1000XM5 Headphones">'
        '<meta property="product:price:amount" content="1,299.00">'
    )
    assert parse_offers(meta, "https://www.sony.com/p", "Sony XM5")[0].source == (
        "page_meta"
    )
    assert not parse_offers(
        "<p>Try 30 days for just $1!</p>", "https://www.walmart.com/ip/test", "Sony XM5"
    )
    ranked = rank(
        [
            offer.copy(update={"price": p, "store": s})
            for p, s in [(279.99, "a"), (289.0, "b"), (19.99, "c")]
        ]
    )
    assert [o.price for o in ranked] == [279.99, 289.0]  # accessory-priced outlier
    # A garbled snippet price is dropped when product pages anchor the range.
    mangled = ranked[0].copy(
        update={"source": "search_snippet", "store": "e", "price": 27999.0}
    )
    assert [o.price for o in rank([ranked[0], mangled])] == [279.99]
    assert not fetchable("https://www.bestbuy.com@127.0.0.1/product")
    assert not fetchable("http://www.bestbuy.com/product")


@pytest.mark.parametrize(
    "text, price",
    [
        ("Price $249.00 at our store", 249.0),
        ("Now $1,299.99 with free returns", 1299.99),
        ("Free standard shipping with $35 orders", None),
        ("Save $50 today, limited time", None),
        ("Best earbuds under $300", None),
        ("$10.00/mo for 24 months", None),
        ("Was $299.99, now $249.99", None),
    ],
)
def test_snippet_prices_ignore_thresholds_discounts_and_ambiguity(text, price):
    from laai_agents.merchants import snippet_offer

    offer = snippet_offer(Source(title="x", url="https://shop.example/p", snippet=text), "x")
    assert (offer.price if offer else None) == price


async def test_fetch_refuses_private_network_hosts():
    from laai_agents.merchants import fetch_offers, public_host

    assert not await public_host("127.0.0.1")
    assert not await public_host("localhost")
    assert await fetch_offers("https://127.0.0.1/product", "Sony XM5") == []


async def test_live_failure_does_not_return_demo_catalog(monkeypatch):
    monkeypatch.setenv("PRODUCT_DATA_MODE", "live")

    async def unavailable(req):
        from laai_agents.search import SearchUnavailable

        raise SearchUnavailable("provider down")

    monkeypatch.setattr(live, "compare", unavailable)
    from laai_agents.models import PriceCompareRequest

    result = await handlers.compare_prices(PriceCompareRequest(product="Sony XM5"))
    assert result.data_source == "unavailable" and result.offers == [] and result.error


class ContractFixture(GlassesWorkflow):
    def __init__(self, journal):
        super().__init__("http://localhost:8002", {}, journal)
        self.requests, self.events, self.executions = [], [], 0
        self.fail_result = False

    async def api(self, role, method, path, body=None, params=None):
        self.requests.append((role, method, path, body, params))
        if path.endswith("/events"):
            return {"events": self.events}
        if path.endswith("/display"):
            return {
                "page": {"page_id": "page-sony"},
                "actions": [
                    {"action_id": action_id, "status": "approved"}
                    for action_id in self.state["actions"]
                ],
            }
        if (
            path.endswith("/result")
            and body["status"] == "completed"
            and self.fail_result
        ):
            raise RuntimeError("backend unavailable")
        return {"session_id": "session-1", "revision": 1}

    async def execute(self, plan):
        self.executions += 1
        return "Comparison ready"


PAGE = {
    "page_id": "page-sony",
    "url": "https://example.com/sony",
    "kind": "product",
    "title": "Sony XM5",
    "summary": [],
    "facts": [],
    "source_label": "Browser",
    "updated_at": datetime.now(UTC).isoformat(),
}


async def test_contract_no_execution_before_approval_and_durable_result_retry(tmp_path):
    workflow = ContractFixture(tmp_path / "journal.json")
    await workflow.propose(PAGE, ConsiderRequest(product="Sony XM5", max_price=300))
    assert workflow.executions == 0
    assert [r[0] for r in workflow.requests] == ["web", "web", "agent"]
    proposals = workflow.requests[-1][3]
    assert all(a["requires_approval"] for a in proposals["actions"])
    assert {a["type"] for a in proposals["actions"]} == {
        "compare_prices",
        "find_reviews",
    }
    event = {
        "event_id": "evt-1",
        "cursor": "000001",
        "type": "action.approved",
        "session_id": "session-1",
        "page_id": "page-sony",
        "action_id": next(iter(workflow.state["actions"])),
    }
    workflow.events = [event]
    workflow.fail_result = True
    with pytest.raises(RuntimeError):
        await workflow.poll_once()
    assert workflow.state["cursor"] == "" and workflow.executions == 1
    restarted = ContractFixture(workflow.journal)
    restarted.events = [event]
    await restarted.poll_once()
    assert restarted.executions == 0 and restarted.state["cursor"] == "000001"
    result = [r[3] for r in restarted.requests if r[2].endswith("/result")][-1]
    assert result["request_id"] == "evt-1:outcome" and result["status"] == "completed"


@pytest.mark.parametrize(
    "field,value",
    [("session_id", "other"), ("page_id", "old"), ("action_id", "unknown")],
)
async def test_contract_rejects_cross_context_events(tmp_path, field, value):
    workflow = ContractFixture(tmp_path / "journal.json")
    await workflow.propose(PAGE, ConsiderRequest(product="Sony XM5"))
    event = {
        "event_id": "evt-1",
        "cursor": "000001",
        "type": "action.approved",
        "session_id": "session-1",
        "page_id": "page-sony",
        "action_id": next(iter(workflow.state["actions"])),
    }
    event[field] = value
    workflow.events = [event]
    with pytest.raises(ValueError):
        await workflow.poll_once()
    assert workflow.executions == 0 and workflow.state["cursor"] == ""


def _review_page(name, value, best=None, pros=(), cons=()):
    import json

    rating = {"@type": "Rating", "ratingValue": value}
    if best is not None:
        rating["bestRating"] = best
    review = {
        "@type": "Review",
        "itemReviewed": {"@type": "Product", "name": name},
        "reviewRating": rating,
        "positiveNotes": {
            "@type": "ItemList",
            "itemListElement": [{"@type": "ListItem", "name": p} for p in pros],
        },
        "negativeNotes": [{"@type": "ListItem", "name": c} for c in cons],
    }
    return f'<title>{name} review</title><script type="application/ld+json">{json.dumps(review)}</script>'


def test_expert_review_ratings_are_normalized_and_matched():
    from laai_agents.expert_reviews import parse_review, to_five

    assert to_five("7.7", "10") == 3.9 and to_five(8.6) == 4.3 and to_five(4) == 4.0
    assert to_five(90) == 4.5 and to_five(0) is None and to_five("n/a") is None
    page = _review_page("Sony WH-1000XM5", 4.5, 5, ["Great ANC"], ["Pricey"])
    rating, pros, cons = parse_review(page, "https://www.pcmag.com/r", "Sony XM5")
    assert (rating.source, rating.rating, pros, cons) == ("pcmag.com", 4.5, ["Great ANC"], ["Pricey"])
    # A review of a different model on the same page is not counted.
    other = _review_page("Sony WH-1000XM4", 5).replace("XM4 review", "XM4 review")
    assert parse_review(other, "https://x.example/r", "Sony XM5")[0] is None


async def test_reviews_average_expert_ratings(monkeypatch):
    from laai_agents import merchants
    from laai_agents.models import ReviewsRequest

    pages = {
        "https://www.pcmag.com/xm5": _review_page("Sony WH-1000XM5", 4.5, 5, ["Great ANC"]),
        "https://www.soundguys.com/xm5": _review_page("Sony WH-1000XM5", 8.6, 10, [], ["Pricey"]),
        "https://www.rtings.com/xm5": "<title>Sony WH-1000XM5 review</title>",
    }

    async def search(query):
        sources = [Source(title="Sony WH-1000XM5 review", url=u) for u in pages]
        return sources, {"sources": sources, "data_source": "ddgs", "notice": "n"}

    async def fetch_page(url):
        return pages[url]

    monkeypatch.setattr(live, "search", search)
    monkeypatch.setattr(merchants, "fetch_page", fetch_page)
    result = await live.reviews(ReviewsRequest(product="Sony XM5"))
    assert result.rating == 4.4 and result.review_count == 2
    assert [r.source for r in result.ratings] == ["pcmag.com", "soundguys.com"]
    assert result.pros == ["Great ANC"] and result.cons == ["Pricey"]
    assert "4.4/5" in result.summary


def test_snippet_prices_skip_listing_pages_and_copies():
    from laai_agents.merchants import listing_page, rank, snippet_offer
    from laai_agents.models import Offer

    assert listing_page("https://www.walmart.com/brand/nothing/20006807")
    assert listing_page("https://www.amazon.com/s?k=airpods")
    assert listing_page("https://www.ebay.com/sch/i.html?_nkw=xm5")
    assert not listing_page("https://www.walmart.com/ip/Nothing-Ear-a/123")
    assert not listing_page("https://www.apple.com/shop/buy-airpods/airpods-pro-2")
    brand = Source(title="Nothing Ear (a)", url="https://www.walmart.com/brand/nothing/1", snippet="Now $79.00")
    assert snippet_offer(brand, "Nothing Ear (a)") is None

    page = Offer(product="p", store="us.smartprix.com", price=249.31, url="https://a.example", source="structured", trust="medium")
    copied = Offer(product="p", store="target.com", price=249.31, url="https://b.example", source="search_snippet", trust="medium")
    own = Offer(product="p", store="bestbuy.com", price=239.0, url="https://c.example", source="search_snippet", trust="medium")
    assert [o.store for o in rank([page, copied, own])] == ["bestbuy.com", "us.smartprix.com"]


def test_review_notes_merge_same_topic_and_sites_count_once():
    assert live.merge_notes(["Expensive"], ["Price", "Pricey", "No IP rating"]) == ["Expensive", "No IP rating"]
    assert live.merge_notes(["Great ANC"], ["Excellent noise cancellation for the price"]) == [
        "Great ANC",
        "Excellent noise cancellation for the price",
    ]


async def test_reviews_count_each_site_once(monkeypatch):
    from laai_agents import merchants
    from laai_agents.models import ReviewsRequest

    pages = {
        "https://www.soundguys.com/q45-review": _review_page("Anker Soundcore Space Q45", 7.2, 10),
        "https://www.soundguys.com/product/q45": _review_page("Anker Soundcore Space Q45", 7.2, 10),
        "https://www.pcmag.com/q45": _review_page("Anker Soundcore Space Q45", 4.0, 5),
    }

    async def search(query):
        sources = [Source(title="Anker Soundcore Space Q45 review", url=u) for u in pages]
        return sources, {"sources": sources, "data_source": "ddgs", "notice": "n"}

    async def fetch_page(url):
        return pages[url]

    monkeypatch.setattr(live, "search", search)
    monkeypatch.setattr(merchants, "fetch_page", fetch_page)
    result = await live.reviews(ReviewsRequest(product="Space Q45"))
    assert [r.source for r in result.ratings] == ["soundguys.com", "pcmag.com"]
    assert result.review_count == 2 and result.rating == 3.8  # (3.6 + 4.0) / 2
