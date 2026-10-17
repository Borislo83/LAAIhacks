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


async def test_live_prices_reject_other_variants_and_ambiguous_prices(monkeypatch):
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
        ]
        return sources, {
            "sources": sources,
            "data_source": "ddgs",
            "notice": "Search lead",
        }

    monkeypatch.setattr(live, "search", search)
    result = await live.compare(type("Request", (), {"product": "Sony XM5"})())
    # A search snippet, even a plausible one, is not a structured merchant offer.
    assert result.offers == [] and result.best is None
    assert result.data_source == "ddgs"


def test_merchant_requires_matching_in_stock_usd_offer():
    import json

    from laai_agents.merchants import allowed, parse_offers

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

    assert (
        parse_offers(page(product), "https://www.bestbuy.com/product/test", "Sony XM5")[
            0
        ].price
        == 279.99
    )
    product["name"] = "Sony WH-1000XM4"
    assert not parse_offers(
        page(product), "https://www.bestbuy.com/product/test", "Sony XM5"
    )
    product["name"] = "Sony WH-1000XM5"
    product["offers"]["priceCurrency"] = "CAD"
    assert not parse_offers(
        page(product), "https://www.bestbuy.com/product/test", "Sony XM5"
    )
    assert not parse_offers(
        "<p>Try 30 days for just $1!</p>", "https://www.walmart.com/ip/test", "Sony XM5"
    )
    assert not allowed("https://www.bestbuy.com.evil.example/product")
    assert not allowed("https://www.bestbuy.com@127.0.0.1/product")
    assert not allowed("http://www.bestbuy.com/product")


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
