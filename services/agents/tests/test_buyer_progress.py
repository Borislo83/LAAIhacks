import asyncio
from uuid import uuid4

import pytest
from laai_agents import buyer, handlers, progress
from laai_agents.models import PriceCompareRequest, ReviewsRequest, Source


@pytest.mark.parametrize(
    "url",
    [
        "http://www.amazon.com/dp/B0B5VHRX7F",
        "https://amazon.com.evil.example/dp/B0B5VHRX7F",
        "https://www.amazon.com@127.0.0.1/dp/B0B5VHRX7F",
        "https://www.amazon.com/s?k=headphones",
    ],
)
def test_buyer_only_accepts_real_https_amazon_listing_shape(url):
    assert buyer.amazon_listing(url) is None


async def test_buying_url_prepares_review_and_real_cart_confirmation_is_idempotent(
    monkeypatch,
):
    async def never_search(query):
        pytest.fail("A supplied listing should not trigger a web search or checkout")

    monkeypatch.setattr(buyer, "search", never_search)
    monkeypatch.setenv("AMAZON_CART_EXECUTOR_TOKEN", "test-executor-token")
    from laai_agents import amazon_cart

    calls = []

    async def add_to_cart(url, quantity):
        calls.append((url, quantity))
        return {
            "status": "cart_added",
            "cart_url": "https://www.amazon.com/gp/cart/view.html",
            "message": "Item verified in Amazon cart. No real order was placed.",
        }

    monkeypatch.setattr(amazon_cart, "add_to_cart", add_to_cart)
    prepared = await buyer.prepare(
        buyer.BuyRequest(
            product="Sony XM5",
            url="https://www.amazon.com/name/dp/B0B5VHRX7F?tag=ignored",
            quantity=2,
        )
    )
    assert prepared.status == "cart_review" and prepared.purchase_enabled is False
    assert prepared.selected_url == "https://www.amazon.com/dp/B0B5VHRX7F"
    assert prepared.quantity == 2 and prepared.confirmation_token
    assert calls == []
    req = buyer.BuyConfirmation(
        confirmation_token=prepared.confirmation_token,
        executor_token="test-executor-token",
    )
    first, retry = await buyer.confirm(req), await buyer.confirm(req)
    assert first.status == "cart_added" and first.purchase_enabled is False
    assert first.cart_url == retry.cart_url
    assert calls == [(prepared.selected_url, 2)]
    assert "no real order" in first.summary.lower()
    assert not hasattr(first, "order_id")


async def test_product_search_requires_listing_selection_and_does_not_prepare_order(
    monkeypatch,
):
    async def search(query):
        assert "site:amazon.com" in query and "Sony WH-1000XM5" in query
        sources = [
            Source(title="Sony XM5", url="https://www.amazon.com/dp/B0B5VHRX7F"),
            Source(title="Other", url="https://amazon.com.evil.example/dp/B0B5VHRX7F"),
        ]
        return sources, {"data_source": "ddgs", "sources": sources}

    monkeypatch.setattr(buyer, "search", search)
    monkeypatch.setenv("AMAZON_CART_EXECUTOR_TOKEN", "test-executor-token")
    result = await buyer.prepare(buyer.BuyRequest(product="Sony XM5"))
    assert result.status == "choose_listing" and len(result.candidates) == 1
    assert result.confirmation_token is None
    assert (
        await buyer.confirm(
            buyer.BuyConfirmation(
                confirmation_token=uuid4().hex, executor_token="test-executor-token"
            )
        )
    ).status == "expired"


async def test_cart_executor_auth_and_uncertain_result_never_retries_mutation(
    monkeypatch,
):
    from laai_agents import amazon_cart

    monkeypatch.setenv("AMAZON_CART_EXECUTOR_TOKEN", "test-executor-token")
    calls = []

    async def uncertain(url, quantity):
        calls.append(url)
        return {
            "status": "cart_unverified",
            "message": "Inspect Amazon before retrying",
        }

    monkeypatch.setattr(amazon_cart, "add_to_cart", uncertain)
    prepared = await buyer.prepare(
        buyer.BuyRequest(url="https://www.amazon.com/dp/B0B5VHRX7F")
    )
    invalid = await buyer.confirm(
        buyer.BuyConfirmation(confirmation_token=prepared.confirmation_token)
    )
    assert invalid.status == "not_connected" and calls == []
    req = buyer.BuyConfirmation(
        confirmation_token=prepared.confirmation_token,
        executor_token="test-executor-token",
    )
    assert (await buyer.confirm(req)).status == "cart_unverified"
    assert (await buyer.confirm(req)).status == "cart_unverified"
    assert len(calls) == 1


async def test_real_handler_progress_is_isolated_and_cursored(monkeypatch):
    monkeypatch.setenv("PRODUCT_DATA_MODE", "demo")
    monkeypatch.delenv("ASI_ONE_API_KEY", raising=False)
    price_id, reviews_id = uuid4().hex, uuid4().hex
    await asyncio.gather(
        handlers.compare_prices(
            PriceCompareRequest(product="Sony XM5", progress_id=price_id)
        ),
        handlers.get_reviews(
            ReviewsRequest(product="Sony XM5", progress_id=reviews_id)
        ),
    )
    events = progress.read(price_id, 0)
    assert [e["stage"] for e in events] == ["started", "completed"]
    assert {e["kind"] for e in events} == {"price"}
    assert events[-1]["result"]["best"]["price"] == 328
    assert progress.read(price_id, events[0]["seq"]) == events[1:]
    assert {e["kind"] for e in progress.read(reviews_id, 0)} == {"reviews"}
    assert progress.read(uuid4().hex, 0) == []
