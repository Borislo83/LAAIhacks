"""Price extraction fixes and the deadline/cache behavior, all on fixture data."""

import asyncio
import json
import time

import pytest
from conftest import REAL_WALMART_PAGE_SEARCH

from laai_agents import buyer, live, merchants, stores
from laai_agents.models import Offer, PriceCompareRequest, Source


def _walmart_item(name, price, seller="Walmart.com", code=1, path=None, **extra):
    return {
        "__typename": "Product",
        "name": name,
        "canonicalUrl": path or "/ip/" + name.replace(" ", "-")[:40] + "/123?athbdg=L1",
        "price": price,
        "sellerName": seller,
        "conditionV2": {"code": code},
        "isPreowned": False,
        "isOutOfStock": False,
        "averageRating": 4.6,
        "numberOfReviews": 1200,
        "priceInfo": {
            "priceDetails": {
                "currency": "USD",
                "priceLines": [
                    {"lineType": "CURRENT_PRICE", "values": [{"key": "PRICE", "value": f"{price:.2f}"}]}
                ],
            }
        },
        **extra,
    }


WALMART_PAGE = (
    '<html><head><title>Walmart</title></head><body><script id="__NEXT_DATA__" type="application/json">'
    + json.dumps(
        {
            "props": {
                "pageProps": {
                    "initialData": {
                        "searchResult": {
                            "itemStacks": [
                                {
                                    "items": [
                                        _walmart_item("Sony WH-1000XM5 Wireless Headphones, Black", 298.0),
                                        _walmart_item("Sony WH1000XM5/B Wireless Industry Leading Headphones",
                                                      319.99, seller="Beach Camera"),
                                        _walmart_item("Restored Sony WH-1000XM5 Headphones", 199.0,
                                                      seller="Beach Camera", code=5),
                                        _walmart_item("Sony WH-1000XM6 Headphones", 458.0),
                                        _walmart_item("Sony WH-1000XM5 Black", 279.0, isOutOfStock=True),
                                    ]
                                }
                            ]
                        }
                    }
                }
            }
        }
    )
    + "</script></body></html>"
)


def test_walmart_search_page_yields_labeled_listings():
    offers = stores.parse_walmart_results(WALMART_PAGE)
    by_price = {o.price: o for o in offers}
    assert set(by_price) == {298.0, 319.99, 199.0, 458.0}  # out of stock dropped
    own, market, restored = by_price[298.0], by_price[319.99], by_price[199.0]
    assert (own.trust, own.seller, own.condition) == ("high", None, "new")
    assert own.url.startswith("https://www.walmart.com/ip/") and "?" not in own.url
    assert (market.trust, market.seller) == ("medium", "Beach Camera")
    assert "Marketplace seller" in market.note
    assert restored.condition == "used/refurb"
    assert stores.parse_walmart_results("<html>blocked</html>") == []
    assert stores.parse_walmart_results(WALMART_PAGE, max_price=300) and all(
        o.price < 300 for o in stores.parse_walmart_results(WALMART_PAGE, max_price=300)
    )


async def test_walmart_reader_uses_the_safe_fetcher(monkeypatch):
    asked = []

    async def fetch(url, limit=merchants.LIMIT, timeout=None):
        asked.append(url)
        return WALMART_PAGE

    monkeypatch.setattr(merchants, "fetch_page", fetch)
    offers = await REAL_WALMART_PAGE_SEARCH("Sony WH-1000XM5")
    assert offers and asked == ["https://www.walmart.com/search?q=Sony+WH-1000XM5"]
    await REAL_WALMART_PAGE_SEARCH("Sony WH-1000XM5")
    assert len(asked) == 1  # cached


@pytest.mark.parametrize(
    "title, matches",
    [
        ("Sony WH1000XM5/B Wireless Industry Leading Headphones", True),
        ("Sony WH1000XM5P Noise-Canceling Over-Ear Headphones - Pink", True),
        ("Sony WH-1000XM5 The Best Wireless Noise Canceling Headphones", True),
        ("Sony WH1000XM6 Wireless Headphones", False),
        ("Sony WH1000XM4 Wireless Headphones", False),
        ("Sony WH1000XM55 Headphones", False),
    ],
)
def test_glued_model_numbers_match_only_the_same_model(title, matches):
    assert live.matches_product(title, "Sony WH-1000XM5") is matches


def test_used_listings_are_labeled_not_dropped_and_never_best():
    assert merchants.condition_of("Restored Sony WH-1000XM5") == "used/refurb"
    assert merchants.condition_of("Amazon Renewed WH-1000XM5/B") == "used/refurb"
    assert merchants.condition_of("(Open Box) Sony WH-1000XM5") == "used/refurb"
    assert merchants.condition_of("Sony WH-1000XM5, used by pros") == "new"
    assert not merchants.is_accessory("Restored Sony WH-1000XM5 Headphones", "Sony WH-1000XM5")
    offers = merchants.rank([
        Offer(product="Restored Sony WH-1000XM5", store="walmart.com", price=199, url="https://w/1",
              source="walmart_search_page", condition="used/refurb"),
        Offer(product="Sony WH-1000XM5", store="walmart.com", price=298, url="https://w/2",
              source="walmart_search_page"),
        Offer(product="Sony WH-1000XM5", store="shop.example", price=279, url="https://s/1",
              source="search_snippet", trust="low"),
    ])  # fmt: skip
    assert [(o.price, o.verified) for o in offers] == [(298, True), (199, False), (279, False)]
    assert offers[1].note == "Used or refurbished listing"
    assert "search listing" in offers[2].note
    assert merchants.best_offer(offers).price == 298


async def test_compare_returns_many_labeled_offers_with_a_verified_headline(monkeypatch):
    async def search(query):
        sources = [Source(title="Sony WH-1000XM5", url="https://shop.example/xm5", snippet="Now $289.00")]
        return sources, {"sources": sources, "data_source": "ddgs", "notice": "Leads."}

    async def no_pages(url, product):
        return []

    async def walmart(query, max_price=None):
        return stores.parse_walmart_results(WALMART_PAGE, max_price)

    monkeypatch.setattr(live, "search", search)
    monkeypatch.setattr(merchants, "fetch_offers", no_pages)
    monkeypatch.setattr(stores, "walmart_page_search", walmart)
    result = await live.compare(PriceCompareRequest(product="Sony WH-1000XM5"))
    assert [(o.price, o.trust, o.condition) for o in result.offers] == [
        (298.0, "high", "new"), (319.99, "medium", "new"), (199.0, "medium", "used/refurb"),
        (289.0, "low", "new"),
    ]  # fmt: skip
    assert result.best.price == 298.0 and result.best.verified
    assert result.summary.startswith("4 offers, 1 verified: best $298.00 at Walmart (verified).")
    assert "Lowest unverified lead: $289.00 at shop.example" in result.summary
    assert result.partial is False


async def test_compare_answers_by_the_deadline_and_caches(monkeypatch):
    async def slow_search(query):
        await asyncio.sleep(30)

    async def walmart(query, max_price=None):
        return stores.parse_walmart_results(WALMART_PAGE, max_price)

    monkeypatch.setattr(live, "search", slow_search)
    monkeypatch.setattr(stores, "walmart_page_search", walmart)
    monkeypatch.setattr(live, "PRICE_DEADLINE", 0.2)
    started = time.monotonic()
    result = await live.compare(PriceCompareRequest(product="Sony WH-1000XM5"))
    assert time.monotonic() - started < 1
    assert result.partial and "may be incomplete" in result.notice
    assert result.best.price == 298.0  # the store listings that did arrive still count

    async def fail(*args, **kwargs):
        raise AssertionError("a repeat should come from the cache")

    monkeypatch.setattr(live, "search", fail)
    monkeypatch.setattr(stores, "walmart_page_search", fail)
    again = await live.compare(PriceCompareRequest(product="sony wh-1000xm5"))
    assert again.best.price == 298.0


async def test_snippet_lead_is_replaced_by_the_price_read_from_its_page(monkeypatch):
    async def search(query):
        sources = [Source(title="Sony WH-1000XM5", url="https://www.target.com/p/xm5", snippet="$279.99")]
        return sources, {"sources": sources, "data_source": "ddgs", "notice": None}

    async def page(url, product):
        return [Offer(product=product, store="target.com", price=279.99, url=url, source="structured")]

    monkeypatch.setattr(live, "search", search)
    monkeypatch.setattr(merchants, "fetch_offers", page)
    result = await live.compare(PriceCompareRequest(product="Sony WH-1000XM5"))
    assert [(o.source, o.verified) for o in result.offers] == [("structured", True)]


async def test_blocked_store_pages_are_not_waited_on_again(monkeypatch):
    calls = []

    async def refused(url, limit=merchants.LIMIT, timeout=None):
        calls.append(url)

    monkeypatch.setattr(merchants, "fetch_page", refused)
    assert await merchants.fetch_offers("https://www.bestbuy.com/p/1", "Sony XM5") == []
    assert calls == ["https://www.bestbuy.com/p/1"]
    assert merchants.blocked("https://www.bestbuy.com/p/2")
    assert not merchants.blocked("https://www.walmart.com/ip/1")


def test_buyer_accepts_amazon_landing_page_links():
    assert buyer.amazon_listing("https://www.amazon.com/clp/B09XS7JWHH") == (
        "https://www.amazon.com/dp/B09XS7JWHH"
    )
    assert buyer.amazon_listing("https://www.amazon.com/sony-wh1000xm5/s?k=sony+wh1000xm5") is None


async def test_buyer_lists_several_real_amazon_candidates(monkeypatch):
    async def search(query):
        sources = [
            Source(title="Amazon.com: Sony WH-1000XM5 Premium Noise Cancelling",
                   url="https://www.amazon.com/clp/B09XS7JWHH"),
            Source(title="Amazon.com: Sony Wh1000xm5", url="https://www.amazon.com/sony-wh1000xm5/s?k=x"),
            Source(title="Amazon.com: Bose QuietComfort", url="https://www.amazon.com/clp/B0CCZ26B5V"),
        ]  # fmt: skip
        return sources, {"data_source": "ddgs", "sources": sources}

    async def page(query, max_price=None):
        return [
            stores._offer("Sony WH-1000XM5 Wireless Headphones, Black", "amazon.com", 328.0,
                          "https://www.amazon.com/dp/B0BXYCS74H", None, 4.5, "100", "amazon_search_page"),
            stores._offer("Sony WH-1000XM5 Renewed", "amazon.com", 199.0,
                          "https://www.amazon.com/dp/B0B2FCT81R", None, 4.3, "50", "amazon_search_page"),
            stores._offer("Sony WH-1000XM6 Headphones", "amazon.com", 458.0,
                          "https://www.amazon.com/dp/B0F1234567", None, 4.6, "50", "amazon_search_page"),
        ]  # fmt: skip

    monkeypatch.setattr(buyer, "search", search)
    monkeypatch.setattr(stores, "amazon_page_search", page)
    result = await buyer.prepare(buyer.BuyRequest(product="Sony WH-1000XM5"))
    assert result.status == "choose_listing" and result.confirmation_token is None
    assert [c["url"].rsplit("/", 1)[1] for c in result.candidates] == [
        "B0BXYCS74H", "B0B2FCT81R", "B09XS7JWHH",
    ]  # fmt: skip
    assert result.candidates[0]["price"] == 328.0 and result.candidates[1]["condition"] == "used/refurb"
    assert result.purchase_enabled is False


def _walmart_product_page(seller="Video & Audio Center", price=288, new=True, status="IN_STOCK"):
    product = {
        "name": "Sony WH-1000XM5 The Best Wireless Noise Canceling Headphones, Black",
        "sellerDisplayName": seller,
        "isConditionNew": new,
        "availabilityStatus": status,
        "priceInfo": {"currentPrice": {"price": price, "currencyUnit": "USD", "priceString": f"${price}.00"}},
    }
    data = {"props": {"pageProps": {"initialData": {"data": {"product": product}}}}}
    return f'<html><script id="__NEXT_DATA__">{json.dumps(data)}</script></html>'


def test_walmart_product_page_price_and_seller_are_read():
    url = "https://www.walmart.com/ip/Sony-WH-1000XM5/386006068?athbdg=L1"
    [offer] = merchants.parse_offers(_walmart_product_page(), url, "Sony WH-1000XM5")
    assert (offer.price, offer.trust, offer.seller) == (288.0, "medium", "Video & Audio Center")
    assert offer.url == "https://www.walmart.com/ip/Sony-WH-1000XM5/386006068"
    [own] = merchants.parse_offers(_walmart_product_page(seller="Walmart.com"), url, "Sony WH-1000XM5")
    assert own.trust == "high" and own.seller is None
    [used] = merchants.parse_offers(_walmart_product_page(new=False), url, "Sony WH-1000XM5")
    assert used.condition == "used/refurb"
    assert merchants.parse_offers(_walmart_product_page(status="OUT_OF_STOCK"), url, "Sony WH-1000XM5") == []


AMAZON_PRODUCT = """<html><body>
<span id="productTitle"> Nintendo Switch 2 System </span>
<div id="corePriceDisplay_desktop_feature_div">
  <span class="a-price"><span class="a-offscreen">$449.00</span></span>
  <span class="a-price a-text-price"><span class="a-offscreen">$499.99</span></span>
</div>
<div id="sims"><span class="a-price"><span class="a-offscreen">$19.99</span></span></div>
</body></html>"""


def test_amazon_product_page_reads_only_the_buy_box_price():
    url = "https://www.amazon.com/dp/B0F3GWXLTS"
    [offer] = merchants.parse_offers(AMAZON_PRODUCT, url, "Nintendo Switch 2")
    assert (offer.price, offer.trust, offer.store) == (449.0, "high", "amazon.com")
    # No buy-box price (not sold new right now): nothing, even with other prices around.
    empty_box = AMAZON_PRODUCT.replace("$449.00", " ")
    assert merchants.parse_offers(empty_box, url, "Nintendo Switch 2") == []
    # Another product's page is not this product.
    assert merchants.parse_offers(AMAZON_PRODUCT, url, "Nintendo Switch OLED") == []


async def test_amazon_landing_links_are_read_as_product_pages(monkeypatch):
    asked = []

    async def fetch(url, limit=merchants.LIMIT, timeout=None):
        asked.append(url)
        return AMAZON_PRODUCT

    monkeypatch.setattr(merchants, "fetch_page", fetch)
    [offer] = await merchants.fetch_offers("https://www.amazon.com/clp/B0F3GWXLTS", "Nintendo Switch 2")
    assert asked == ["https://www.amazon.com/dp/B0F3GWXLTS"] and offer.price == 449.0


# --- Generations and tiers -------------------------------------------------------------


@pytest.mark.parametrize(
    "title, product, matches",
    [
        ("Apple AirPods Pro 3 Wireless Earbuds", "AirPods Pro 3", True),
        ("Apple AirPods Pro (3rd Generation)", "AirPods Pro 3", True),
        ("AirPods Pro Gen 3", "AirPods Pro 3", True),
        ("Apple AirPods Pro 2 Wireless Earbuds", "AirPods Pro 3", False),
        ("Apple AirPods Pro (2nd generation) with MagSafe Case (USB-C)", "AirPods Pro 3", False),
        ("Apple AirPods Pro (2nd Gen)", "AirPods Pro 2", True),
        ("Apple AirPods Pro, second generation", "AirPods Pro 2", True),
        ("Apple AirPods 4 Wireless Earbuds", "AirPods 4", True),
        ("Apple AirPods Pro 2", "AirPods 4", False),
        ("Apple AirPods Pro 4", "AirPods 4", False),
        ("Apple AirPods 4 with Active Noise Cancellation", "AirPods Pro", False),
        ("Nintendo Switch 2 System", "Nintendo Switch 2", True),
        ("Nintendo Switch OLED Model", "Nintendo Switch 2", False),
        ("Nintendo Switch Lite", "Nintendo Switch 2", False),
        ("Nintendo Switch 2 System", "Nintendo Switch OLED", False),
    ],
)
def test_generation_and_tier_words_are_part_of_the_model(title, product, matches):
    assert live.matches_product(title, product) is matches
    # The looser review/buyer matcher never accepts another generation or tier either.
    if not matches:
        assert not live.names_product(title, product)


def test_catalog_does_not_turn_a_newer_generation_into_the_older_one():
    from laai_agents import catalog

    assert catalog.find_product("AirPods Pro 3") is None
    assert live.product_name("AirPods Pro 3") == "AirPods Pro 3"
    assert catalog.find_product("AirPods Pro 2nd generation").name == "Apple AirPods Pro 2"
    # A budget is not a model number.
    assert catalog.find_product("Sony XM5 under $300").name == "Sony WH-1000XM5"


def test_listing_without_a_generation_is_unconfirmed_not_matched():
    assert live.unconfirmed_model("Apple AirPods Pro with MagSafe Case", "AirPods Pro 3")
    assert not live.unconfirmed_model("Apple AirPods Pro 2", "AirPods Pro 3")
    assert not live.unconfirmed_model("Apple AirPods Pro 3", "AirPods Pro 3")


async def test_airpods_pro_3_prices_never_use_pro_2_listings(monkeypatch):
    async def search(query):
        assert "AirPods Pro 3" in query
        sources = [
            Source(title="Apple AirPods Pro (2nd generation) with MagSafe Case (USB-C)",
                   url="https://www.target.com/p/airpods-pro-2nd-generation/-/A-85978612", snippet="$199.99"),
            Source(title="Apple AirPods Pro 3", url="https://shop.example/pro3", snippet="Now $239.00"),
            Source(title="Apple AirPods Pro", url="https://deals.example/pro", snippet="Only $169.00"),
        ]  # fmt: skip
        return sources, {"sources": sources, "data_source": "ddgs", "notice": None}

    pages = []

    async def page(url, product):
        pages.append(url)
        return []

    async def walmart(query, max_price=None):
        return [
            stores._offer("Apple AirPods Pro 3 Wireless Earbuds", "walmart.com", 249.0,
                          "https://www.walmart.com/ip/1", None, 4.7, "900", "walmart_search_page"),
            stores._offer("Apple AirPods Pro 2 Wireless Earbuds", "walmart.com", 189.0,
                          "https://www.walmart.com/ip/2", None, 4.8, "9000", "walmart_search_page"),
            stores._offer("Apple AirPods Pro Wireless Earbuds", "walmart.com", 229.0,
                          "https://www.walmart.com/ip/3", None, 4.5, "100", "walmart_search_page"),
        ]  # fmt: skip

    monkeypatch.setattr(live, "search", search)
    monkeypatch.setattr(merchants, "fetch_offers", page)
    monkeypatch.setattr(stores, "walmart_page_search", walmart)
    result = await live.compare(PriceCompareRequest(product="AirPods Pro 3"))
    assert result.product == "AirPods Pro 3"
    prices = {o.price: o for o in result.offers}
    assert 189.0 not in prices and 199.99 not in prices  # Pro 2 dropped
    assert result.best.price == 249.0 and result.best.verified
    for price in (229.0, 169.0):  # no generation stated: kept, labeled, never verified
        assert prices[price].note == merchants.MODEL_UNCONFIRMED and not prices[price].verified
    assert pages == ["https://shop.example/pro3"]


async def test_older_generation_is_labeled_not_listed_as_a_rival(monkeypatch):
    async def search(query):
        sources = [Source(title=t, url=f"https://reviews.example/{i}") for i, t in enumerate([
            "AirPods Pro 3 vs AirPods Pro 2: should you upgrade?",
            "AirPods Pro 2 vs AirPods Pro 3 compared",
            "AirPods Pro 3 vs Sony WF-1000XM5",
            "Sony WF-1000XM5 vs AirPods Pro 3 review",
        ])]  # fmt: skip
        return sources, {"sources": sources, "data_source": "ddgs", "notice": None}

    async def compare(req):
        return type("R", (), {"best": None, "sources": []})()

    monkeypatch.setattr(live, "search", search)
    monkeypatch.setattr(live, "compare", compare)
    priced, unpriced = await live.compared_alternatives("AirPods Pro 3", None)
    reasons = {a.product: a.reason for a in priced + unpriced}
    assert reasons["AirPods Pro 2"].startswith("Older model of AirPods Pro 3, not a rival")
    assert reasons["Sony WF-1000XM5"].startswith("Often compared with AirPods Pro 3")


def test_search_races_only_engines_this_ddgs_has():
    from ddgs.engines import ENGINES

    from laai_agents import search as search_module

    assert search_module.RACE and set(search_module.RACE) <= set(ENGINES["text"])


def test_a_case_named_after_the_product_is_an_accessory():
    product = "AirPods Pro 3"
    assert merchants.is_accessory("CASETiFY Red Cherry AirPods Pro 3 Case with Ring", product)
    assert merchants.is_accessory("Spigen AirPods Pro 3 Silicone Cover", product)
    assert not merchants.is_accessory("Apple AirPods Pro 3 Wireless Earbuds with MagSafe Charging Case", product)


def test_another_tier_is_a_rival_not_a_newer_model():
    assert live.other_generation("AirPods Pro 2", "AirPods Pro 3") == "older model"
    assert live.other_generation("AirPods 5", "AirPods Pro 3") is None
    assert live.other_generation("Sony WF-1000XM5", "AirPods Pro 3") is None
