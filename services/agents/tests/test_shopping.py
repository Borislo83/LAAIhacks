"""Store listings, cart links, picks, and request parsing for the shopping flow."""

import pytest
from laai_agents import coordinator, live, stores
from laai_agents.carts import amazon_cart_link, bestbuy_cart_link
from laai_agents.models import (
    Alternative,
    AlternativesResponse,
    Offer,
    PriceCompareResponse,
    ReviewsResponse,
)


def test_cart_links_are_official_and_validated():
    assert amazon_cart_link("b09xs7jwhh", 2) == (
        "https://www.amazon.com/gp/aws/cart/add.html?ASIN.1=B09XS7JWHH&Quantity.1=2"
    )
    assert amazon_cart_link("not-an-asin") is None and amazon_cart_link("B09XS7JWHH", 99) is None
    assert bestbuy_cart_link(6505727) == "https://api.bestbuy.com/click/-/6505727/cart"
    assert bestbuy_cart_link("12; rm -rf") is None


async def test_store_offers_parse_bestbuy_and_serpapi(monkeypatch):
    monkeypatch.setenv("BESTBUY_API_KEY", "bb")
    monkeypatch.setenv("SERPAPI_API_KEY", "sp")
    calls = []

    async def fake_get(url, params):
        calls.append((url, params))
        if "bestbuy" in url:
            assert "salePrice<300.00" in url and "search=sony&search=headphones" in url.lower()
            return {"products": [
                {"sku": 6505727, "name": "Sony WH-1000XM5 Headphones", "salePrice": 279.99,
                 "url": "https://www.bestbuy.com/site/6505727.p", "customerReviewAverage": 4.7,
                 "customerReviewCount": 3200, "onlineAvailability": True},
                {"sku": 1, "name": "Sold out", "salePrice": 99, "onlineAvailability": False},
            ]}
        if params["engine"] == "amazon":
            return {"organic_results": [
                {"asin": "B0BXYCS74H", "title": "Sony WH-1000XM5 Midnight Blue", "extracted_price": 248.0,
                 "rating": 4.4, "reviews": 9000},
                {"asin": "B0OVERBUDG", "title": "Sony headphones deluxe", "extracted_price": 450.0},
            ]}
        return {"shopping_results": [
            {"title": "Sony WH-1000XM5", "source": "Walmart", "extracted_price": 259.0,
             "product_link": "https://www.walmart.com/ip/1", "rating": 4.6, "reviews": 120},
            {"title": "Sony WH-1000XM5", "source": "Amazon.com", "extracted_price": 248.0,
             "product_link": "https://www.google.com/shopping/x"},
        ]}

    monkeypatch.setattr(stores, "_get_json", fake_get)
    offers = await stores.store_offers("Sony headphones", 300)
    by_store = {o.store: o for o in offers}
    assert set(by_store) == {"bestbuy.com", "amazon.com", "Walmart"}
    assert by_store["bestbuy.com"].cart_url == "https://api.bestbuy.com/click/-/6505727/cart"
    assert by_store["amazon.com"].cart_url.endswith("ASIN.1=B0BXYCS74H&Quantity.1=1")
    assert by_store["Walmart"].cart_url is None  # other stores: shown, no cart button
    assert all(o.price < 300 for o in offers)


async def test_store_offers_are_empty_without_keys(monkeypatch):
    monkeypatch.delenv("BESTBUY_API_KEY", raising=False)
    monkeypatch.delenv("SERPAPI_API_KEY", raising=False)
    assert await stores.store_offers("Sony headphones", 300) == []


def _offer(name, store, price, rating=None, cart=None, trust="high"):
    return Offer(product=name, store=store, price=price, url=f"https://{store}/p",
                 source="bestbuy_api", trust=trust, cart_url=cart, rating=rating)


def test_picks_are_best_price_alternative_and_recommended_within_budget():
    price = PriceCompareResponse(
        product="Sony headphones", summary="s",
        offers=[
            _offer("Sony CH-720N", "bestbuy.com", 99.99, 4.1, "https://api.bestbuy.com/click/-/1/cart"),
            _offer("Sony WH-1000XM5", "amazon.com", 248.0, 4.6,
                   "https://www.amazon.com/gp/aws/cart/add.html?ASIN.1=B0BXYCS74H&Quantity.1=1"),
            _offer("Sony ULT Wear", "Walmart", 149.0, 4.5),
            _offer("Sony Over Budget", "bestbuy.com", 349.0, 4.9),
        ],
    )
    alts = AlternativesResponse(
        product="Sony headphones", summary="a",
        alternatives=[Alternative(product="Bose QC45", price=199.0, rating=4.4, url="https://bestbuy.com/b",
                                  store="bestbuy.com", cart_url="https://api.bestbuy.com/click/-/2/cart",
                                  reason="4.4★ on bestbuy.com")],
    )
    picks = coordinator.make_picks({"price": price, "alternatives": alts}, 300)
    assert [p.label for p in picks] == ["Best price", "Alternative", "Recommended"]
    assert picks[0].product == "Sony CH-720N" and picks[0].cart_url
    assert picks[1].product == "Bose QC45"
    # Recommended balances rating/price/trust and differs from the other two.
    assert picks[2].product in {"Sony WH-1000XM5", "Sony ULT Wear"}
    assert all(p.price is None or p.price < 300 for p in picks)


def test_picks_use_expert_rating_when_listing_has_none():
    price = PriceCompareResponse(product="Sony WH-1000XM5", summary="s",
                                 offers=[_offer("Sony WH-1000XM5", "amazon.com", 248.0)])
    reviews = ReviewsResponse(product="Sony WH-1000XM5", summary="r", rating=4.6)
    picks = coordinator.make_picks({"price": price, "reviews": reviews}, None)
    assert picks[0].rating == 4.6


@pytest.mark.parametrize(
    "text, phrase",
    [
        ("Are there Sony headphones under 300 dollars?", "Sony headphones"),
        ("Show me good wireless earbuds under $100 please", "wireless earbuds"),
        ("find a bike on amazon", "bike"),
    ],
)
def test_search_phrase_keeps_only_product_words(text, phrase):
    assert coordinator.search_phrase(text) == phrase


async def test_price_agent_puts_store_listings_first(monkeypatch):
    async def search(query):
        return [], {"sources": [], "data_source": "ddgs", "notice": "n"}

    async def store_offers(query, budget):
        return [_offer("Sony WH-1000XM5 Headphones", "bestbuy.com", 279.99, 4.7,
                       "https://api.bestbuy.com/click/-/6505727/cart"),
                _offer("Sony WH-1000XM5 replacement ear pads", "amazon.com", 19.0)]

    monkeypatch.setattr(live, "search", search)
    monkeypatch.setattr(stores, "store_offers", store_offers)
    result = await live.compare(type("R", (), {"product": "Sony XM5", "max_price": 300})())
    assert [o.store for o in result.offers] == ["bestbuy.com"]  # ear pads dropped
    assert result.best.cart_url and "Best price under $300.00" in result.summary
    assert result.data_source.startswith("store_api")


async def test_store_lookups_are_cached_without_the_key(monkeypatch):
    calls = []

    async def fetch(url, params):
        calls.append(params)
        return {"organic_results": []}

    monkeypatch.setattr(stores, "_fetch_json", fetch)
    monkeypatch.setattr(stores, "_CACHE", {})
    params = {"engine": "amazon", "k": "sony", "api_key": "secret-1"}
    await stores._get_json("https://serpapi.com/search.json", params)
    await stores._get_json("https://serpapi.com/search.json", {**params, "api_key": "secret-2"})
    assert len(calls) == 1
    assert all("secret" not in str(key) for key in stores._CACHE)


def test_budget_phrases_stripped_from_the_query_also_set_the_budget():
    for text in ("Sony headphones less than $300", "Sony headphones max $300"):
        assert coordinator.search_phrase(text) == "Sony headphones"
        assert coordinator.budget_from(text) == 300.0


async def test_store_cache_is_bounded(monkeypatch):
    async def fetch(url, params):
        return {"organic_results": []}

    monkeypatch.setattr(stores, "_fetch_json", fetch)
    monkeypatch.setattr(stores, "_CACHE", {})
    monkeypatch.setattr(stores, "CACHE_SIZE", 3)
    for q in "abcde":
        await stores._get_json("https://serpapi.com/search.json", {"k": q})
    assert [key[1] for key in stores._CACHE] == [(("k", "c"),), (("k", "d"),), (("k", "e"),)]


def test_model_name_max_is_not_a_budget():
    text = "iPhone 16 Pro Max 256GB price"
    assert coordinator.budget_from(text) is None
    assert "Max 256GB" in coordinator.search_phrase(text)
    assert coordinator.budget_from("earbuds maximum of $80") == 80.0
