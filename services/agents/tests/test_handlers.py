import pytest

from laai_agents import catalog, handlers
from laai_agents.models import AlternativesRequest, PriceCompareRequest, ReviewsRequest


@pytest.fixture(autouse=True)
def no_llm(monkeypatch):
    monkeypatch.delenv("ASI_ONE_API_KEY", raising=False)
    monkeypatch.setenv("PRODUCT_DATA_MODE", "demo")


@pytest.mark.parametrize(
    "query, expected",
    [
        ("Sony WH-1000XM5", "Sony WH-1000XM5"),
        ("xm5", "Sony WH-1000XM5"),
        ("I'm looking at the Bose QC Ultra", "Bose QuietComfort Ultra Headphones"),
        ("airpods pro", "Apple AirPods Pro 2"),
    ],
)
def test_find_product(query, expected):
    assert catalog.find_product(query).name == expected


def test_find_product_unknown():
    assert catalog.find_product("toaster") is None
    assert catalog.find_product("") is None


async def test_compare_prices_reports_savings_from_current_store():
    res = await handlers.compare_prices(PriceCompareRequest(product="xm5", current_store="Best Buy"))
    assert res.best.store == "Amazon"
    assert res.savings == pytest.approx(21.99)
    assert [o.price for o in res.offers] == sorted(o.price for o in res.offers)


async def test_compare_prices_when_already_cheapest():
    res = await handlers.compare_prices(PriceCompareRequest(product="xm5", current_price=328.0))
    assert res.savings == 0
    assert "already" in res.summary


async def test_compare_prices_unknown_product():
    res = await handlers.compare_prices(PriceCompareRequest(product="toaster"))
    assert res.offers == [] and res.best is None


async def test_alternatives_are_same_category_and_respect_max_price():
    res = await handlers.find_alternatives(AlternativesRequest(product="Bose QC Ultra", max_price=200))
    names = [a.product for a in res.alternatives]
    assert names == ["Anker Soundcore Space Q45"]


async def test_alternatives_include_better_rated_product():
    res = await handlers.find_alternatives(AlternativesRequest(product="Galaxy Buds3 Pro"))
    reasons = {a.product: a.reason for a in res.alternatives}
    assert "rated 4.7" in reasons["Apple AirPods Pro 2"]
    assert "cheaper" in reasons["Nothing Ear (a)"]


async def test_reviews_fall_back_without_llm():
    res = await handlers.get_reviews(ReviewsRequest(product="airpods pro"))
    assert res.rating == 4.7
    assert "45,120 reviews" in res.summary
    assert res.review_url.startswith("https://")
