""""Are there any smart watches under $50 at Amazon": store listings in a budget."""

from conftest import REAL_AMAZON_PAGE_SEARCH

from laai_agents import live, merchants, stores
from laai_agents.coordinator import listing_picks, parse_request, store_from
from laai_agents.models import Offer, PriceCompareRequest


def card(asin, title, price, stars=None, ratings=None, sponsored=False):
    return f"""
    <div data-component-type="s-search-result" data-asin="{asin}">
      {'<span>Sponsored</span>' if sponsored else ''}
      <a href="/dp/{asin}"><h2><span>{title}</span></h2></a>
      <span class="a-price"><span class="a-offscreen">${price}</span></span>
      <span class="a-price a-text-price"><span class="a-offscreen">$99.99</span></span>
      {f'<span class="a-icon-alt">{stars} out of 5 stars</span>' if stars else ''}
      {f'<a aria-label="{stars} out of 5 stars, rating details" href="#s">s</a>' if stars else ''}
      {f'<a aria-label="{ratings} ratings" href="#r">r</a>' if ratings else ''}
    </div>"""


PAGE = "<html><body>" + "".join([
    card("B0AAAAAAA1", "Sponsored Smart Watch", "39.99", "4.9", "10", sponsored=True),
    card("B0DFZPR9Z4", "XIAOMI Redmi Watch 5 Active", "43.49", "4.5", "17,169"),
    card("B09TR2NNJS", "Smart Watch for Men Women", "17.99", "4.2", "4,961"),
    card("B0AAAAAAA2", "Smart Watch Pro Max", "59.99", "4.8", "900"),
    card("B0AAAAAAA3", "Kids Smart Watch", "25.59", "5.0", "4"),
]) + "</body></html>"  # fmt: skip


def test_amazon_results_skip_sponsored_and_over_budget_cards():
    offers = stores.parse_amazon_results(PAGE, max_price=50)
    assert [o.product for o in offers] == [
        "XIAOMI Redmi Watch 5 Active", "Smart Watch for Men Women", "Kids Smart Watch",
    ]  # fmt: skip
    redmi = offers[0]
    assert (redmi.price, redmi.rating, redmi.review_count) == (43.49, 4.5, 17169)
    assert redmi.url == "https://www.amazon.com/dp/B0DFZPR9Z4"
    assert "ASIN.1=B0DFZPR9Z4" in redmi.cart_url
    assert redmi.source == "amazon_search_page"


def test_amazon_search_url_filters_price_in_cents():
    url = stores.amazon_search_url("smart watches", 50)
    assert url == "https://www.amazon.com/s?k=smart+watches&rh=p_36%3A-5000"


async def test_amazon_page_reader_uses_the_safe_fetcher(monkeypatch):
    asked = []

    async def fetch(url, limit=merchants.LIMIT):
        asked.append((url, limit))
        return PAGE

    monkeypatch.setattr(merchants, "fetch_page", fetch)
    offers = await REAL_AMAZON_PAGE_SEARCH("smart watches", 50)
    assert len(offers) == 3 and asked[0][1] > merchants.LIMIT


async def test_amazon_page_reader_gives_nothing_for_a_captcha(monkeypatch):
    async def fetch(url, limit=merchants.LIMIT):
        return "<html>Enter the characters you see below. captcha</html>"

    monkeypatch.setattr(stores, "AMAZON_RETRY_DELAY", 0)
    monkeypatch.setattr(merchants, "fetch_page", fetch)
    assert await REAL_AMAZON_PAGE_SEARCH("smart watches", 50) == []


async def test_amazon_page_reader_retries_a_short_interstitial(monkeypatch):
    pages = ["<html><head></head><body>Continue shopping</body></html>", PAGE]

    async def fetch(url, limit=merchants.LIMIT):
        return pages.pop(0)

    monkeypatch.setattr(stores, "AMAZON_RETRY_DELAY", 0)
    monkeypatch.setattr(merchants, "fetch_page", fetch)
    assert len(await REAL_AMAZON_PAGE_SEARCH("smart watches", 50)) == 3


async def test_keyless_amazon_search_reads_the_results_page(monkeypatch):
    monkeypatch.delenv("SERPAPI_API_KEY", raising=False)

    async def page_search(query, max_price=None):
        return stores.parse_amazon_results(PAGE, max_price)

    monkeypatch.setattr(stores, "amazon_page_search", page_search)
    offers = await stores.amazon_search("smart watches", 50)
    assert {o.store for o in offers} == {"amazon.com"} and len(offers) == 3


async def test_the_smart_watch_question_becomes_an_amazon_browse():
    req = await parse_request("Are there any smart watches under 50 dollars in amazon")
    assert (req.product, req.max_price, req.store, req.browse) == (
        "smart watches", 50.0, "amazon.com", True,
    )  # fmt: skip


async def test_specific_products_are_still_researched_not_browsed():
    req = await parse_request("Consider Sony XM5 and recommend an alternative under $300.")
    assert not req.browse and req.store is None
    req = await parse_request("AirPods Pro 2 on amazon")
    assert not req.browse and req.store == "amazon.com"


def test_where_the_user_is_shopping_is_not_a_store_limit():
    assert store_from("I'm on Best Buy looking at AirPods Pro 2. Cheaper anywhere?") is None
    assert store_from("wireless earbuds under $30 at best buy") == "bestbuy.com"


async def test_browse_ranks_listings_by_rating_weighted_by_count(monkeypatch):
    async def amazon(query, max_price=None):
        return stores.parse_amazon_results(PAGE, max_price)

    async def bestbuy(query, max_price=None):
        raise AssertionError("Best Buy was not asked for")

    monkeypatch.setattr(stores, "amazon_search", amazon)
    monkeypatch.setattr(stores, "bestbuy_search", bestbuy)
    result = await live.compare(
        PriceCompareRequest(product="smart watches", max_price=50, store="amazon.com", browse=True)
    )
    assert result.offers[0].product == "XIAOMI Redmi Watch 5 Active"
    assert all(o.price < 50 for o in result.offers)
    assert result.summary.startswith("Yes. Amazon has smart watches under $50.")
    assert "rh=p_36" in result.sources[0].url


async def test_browse_says_so_when_nothing_could_be_read(monkeypatch):
    async def nothing(query, max_price=None):
        return []

    monkeypatch.setattr(stores, "amazon_search", nothing)
    result = await live.compare(
        PriceCompareRequest(product="smart watches", max_price=50, store="amazon.com", browse=True)
    )
    assert result.offers == [] and "couldn't read Amazon listings" in result.summary
    assert result.sources  # the filtered Amazon results link is still given


async def test_browse_is_honest_about_stores_it_cannot_read():
    result = await live.compare(
        PriceCompareRequest(product="smart watches", max_price=50, store="walmart.com", browse=True)
    )
    assert result.offers == [] and "not walmart.com" in result.summary


def _offer(name, price, rating, count):
    return Offer(product=name, store="amazon.com", price=price, rating=rating,
                 review_count=count, url=f"https://www.amazon.com/dp/{name}")  # fmt: skip


def test_listing_picks_are_popular_best_rated_and_cheapest():
    picks = listing_picks([
        _offer("A", 43.49, 4.5, 17169), _offer("B", 17.99, 4.2, 4961),
        _offer("C", 25.59, 5.0, 4), _offer("D", 29.99, 4.7, 300),
    ])  # fmt: skip
    assert [(p.label, p.product) for p in picks] == [
        ("Most popular", "A"), ("Best rated", "D"), ("Cheapest", "B"),
    ]  # fmt: skip
    assert picks[0].reason == "4.5★ from 17,169 ratings, $43.49 on amazon.com"
