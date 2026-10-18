from datetime import UTC, datetime

import pytest
from laai_agents import coordinator, handlers, live
from laai_agents.integration import GlassesAPIError, GlassesWorkflow
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
        # Action IDs the API has invalidated (a page change hides them from display).
        self.invalidated = set()
        self.invalidate_during_execution = False

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
                    if action_id not in self.invalidated
                ],
            }
        if path.endswith("/result") and path.split("/")[-2] in self.invalidated:
            raise GlassesAPIError("POST", 409)
        if (
            path.endswith("/result")
            and body["status"] == "completed"
            and self.fail_result
        ):
            raise RuntimeError("backend unavailable")
        return {"session_id": "session-1", "revision": 1}

    async def execute(self, plan):
        self.executions += 1
        if self.invalidate_during_execution:
            self.invalidated.update(self.state["actions"])
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


def approved_event(workflow, event_id="evt-1", cursor="000001", index=0):
    return {
        "event_id": event_id,
        "cursor": cursor,
        "type": "action.approved",
        "session_id": "session-1",
        "page_id": "page-sony",
        "action_id": list(workflow.state["actions"])[index],
    }


async def test_invalidated_action_is_recorded_and_does_not_block_later_events(tmp_path):
    workflow = ContractFixture(tmp_path / "journal.json")
    await workflow.propose(PAGE, ConsiderRequest(product="Sony XM5"))
    stale, fresh = approved_event(workflow), approved_event(workflow, "evt-2", "000002", 1)
    workflow.invalidated.add(stale["action_id"])
    workflow.events = [stale, fresh]
    assert await workflow.poll_once() == 2
    # The invalidated action never runs or posts a result, but the cursor moves past it.
    assert workflow.executions == 1 and workflow.state["cursor"] == "000002"
    assert workflow.state["invalidated"]["evt-1"]["action_id"] == stale["action_id"]
    results = [r[2] for r in workflow.requests if r[2].endswith("/result")]
    assert results and all(stale["action_id"] not in path for path in results)
    # A restart replaying the same events does not reprocess either one.
    restarted = ContractFixture(workflow.journal)
    restarted.events = [stale, fresh]
    await restarted.poll_once()
    assert restarted.executions == 0
    assert not [r for r in restarted.requests if r[2].endswith("/display")]


async def test_action_invalidated_during_execution_advances_cursor(tmp_path):
    workflow = ContractFixture(tmp_path / "journal.json")
    await workflow.propose(PAGE, ConsiderRequest(product="Sony XM5"))
    event = approved_event(workflow)
    workflow.events = [event]
    workflow.invalidate_during_execution = True
    await workflow.poll_once()
    assert workflow.executions == 1 and workflow.state["cursor"] == "000001"
    assert workflow.state["invalidated"]["evt-1"]["action_id"] == event["action_id"]


async def test_result_conflict_for_still_approved_action_keeps_blocking(tmp_path):
    workflow = ContractFixture(tmp_path / "journal.json")
    await workflow.propose(PAGE, ConsiderRequest(product="Sony XM5"))
    workflow.events = [approved_event(workflow)]

    async def conflict(*args, **kwargs):
        raise GlassesAPIError("POST", 409)

    original = workflow.api

    async def api(role, method, path, body=None, params=None):
        if path.endswith("/result") and body["status"] == "completed":
            await conflict()
        return await original(role, method, path, body, params)

    workflow.api = api
    with pytest.raises(GlassesAPIError):
        await workflow.poll_once()
    assert workflow.state["cursor"] == "" and "evt-1" not in workflow.state.get(
        "invalidated", {}
    )
