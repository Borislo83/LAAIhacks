"""Regression tests for the bugs found in the agent audit."""

import pytest
from laai_agents import live, news, youtube
from laai_agents.imessage import tidy
from laai_agents.models import AlternativesRequest, PriceCompareResponse, YouTubeRequest
from laai_agents.search import SearchUnavailable


# 1. The brief kept news with "deal" in it; only shopping deal pages are not stories.
@pytest.mark.parametrize(
    "title, story",
    [
        ("Israel and Hamas reach ceasefire deal", True),
        ("Trump signs trade deal with China", True),
        ("Senate passes budget deal to avert shutdown", True),
        ("Best deals on AirPods this weekend", False),
        ("Black Friday deals you can shop now", False),
        ("Deal of the day: Sony headphones", False),
    ],
)
def test_deal_news_is_kept_but_deal_pages_are_not(title, story):
    assert (news.NOT_A_STORY.search(title) is None) is story


# 2. A channel named only "Official" or "VEVO" must not match every creator.
@pytest.mark.parametrize("channel", ["Official", "VEVO", "Official VEVO", " - Topic"])
def test_empty_channel_names_match_no_one(channel):
    assert not youtube.same_creator(channel, "Markiplier")


# 3. "by" inside a song title is not the artist separator.
@pytest.mark.parametrize(
    "query, title, creator",
    [
        ("Stand by Me", "Stand by Me", None),
        ("Stand by Me by Ben E. King", "Stand by Me", "Ben E. King"),
        ("Blinding Lights by The Weeknd", "Blinding Lights", "The Weeknd"),
        ("Toxic by BoywithUke", "Toxic", "BoywithUke"),
    ],
)
def test_song_titles_with_by(query, title, creator):
    want = youtube.intent(query, "song")
    assert (want["title"], want["creator"]) == (title, creator)


# 6. The message formatter must not break links, brand casing or short texts.
@pytest.mark.parametrize(
    "text, expected",
    [
        ("https://youtu.be/dQw4w9WgXcQ", "https://youtu.be/dQw4w9WgXcQ"),
        ("check this out https://youtu.be/dQw4w9WgXcQ", "Check this out https://youtu.be/dQw4w9WgXcQ"),
        ("iPhone pickup at 5", "iPhone pickup at 5."),
        ("ok", "Ok"),
        ("running 10 minutes late", "Running 10 minutes late."),
        ("see you at 7 :)", "See you at 7 :)"),
    ],
)
def test_message_formatting_keeps_links_and_short_texts(text, expected):
    assert tidy(text) == expected


# 7. A failed category search must not sink the alternatives agent.
async def test_alternatives_survive_a_failed_category_search(monkeypatch):
    async def down(query):
        raise SearchUnavailable("rate limited")

    async def no_price(req):
        return PriceCompareResponse(product=req.product, offers=[], summary="none")

    monkeypatch.setattr(live, "search", down)
    monkeypatch.setattr(live, "compare", no_price)
    result = await live.alternatives(AlternativesRequest(product="Sony XM5", max_price=300))
    assert result.alternatives  # catalog candidates still listed
    assert result.data_source == "unavailable" and result.error is None


# 8. A "latest" request during a search outage says so instead of "no video found".
async def test_latest_reports_search_outage_not_missing_video(monkeypatch):
    async def outage(creator):
        raise SearchUnavailable("rate limited")

    async def nothing(query, *extra):
        return []

    monkeypatch.delenv("YOUTUBE_API_KEY", raising=False)
    monkeypatch.delenv("SERPAPI_API_KEY", raising=False)
    monkeypatch.setattr(youtube, "latest_from_feed", outage)
    monkeypatch.setattr(youtube, "PROVIDERS", (("ddgs_videos", nothing), ("web_search", nothing)))
    result = await youtube.find_video(YouTubeRequest(query="find markipliers latest video", kind="video"))
    assert result.error == "YouTube search unavailable"
    assert "temporarily unavailable" in result.summary


# --- Second audit pass -------------------------------------------------------------------

# lxml raises ParserError (not a ValueError) for pages it can't parse at all.
UNPARSEABLE = "<!-- nothing but a comment -->"


def test_unparseable_amazon_results_page_yields_no_offers():
    from laai_agents import stores

    assert stores.parse_amazon_results(UNPARSEABLE) == []


async def test_unparseable_article_page_keeps_the_search_hit(monkeypatch):
    async def page(url, **_):
        return UNPARSEABLE

    monkeypatch.setattr(news, "fetch_page", page)
    hit = {"url": "https://example.com/a", "title": "Story", "lead": "Lead.", "publisher": "Ex",
           "published_at": None}  # fmt: skip
    assert (await news.read_article(hit))["title"] == "Story"


@pytest.mark.parametrize(
    "title, product",
    [
        ("Apple AirPods Pro 2 Wireless Earbuds, Hearing Aid Feature, for iPhone 15 Pro", "AirPods Pro 2"),
        ("Samsung Galaxy Watch 7 40mm Bluetooth Smartwatch for Galaxy S24 Ultra", "Galaxy Watch 7"),
    ],
)
def test_product_made_for_a_phone_is_not_an_accessory(title, product):
    from laai_agents.merchants import is_accessory

    assert not is_accessory(title, product)
    assert is_accessory(f"ESR Case for {product}", product)


@pytest.mark.parametrize("seller", ["Walmart", "Walmart - Seller", "walmart.com"])
def test_store_filter_accepts_shopping_store_names(seller):
    assert live.sold_at(seller, "walmart.com")
    assert live.sold_at("Best Buy", "bestbuy.com") and not live.sold_at("Target", "walmart.com")


def test_category_words_with_digits_are_still_categories():
    from laai_agents import coordinator

    assert coordinator.is_listing("are there 4k tvs under $300", "4k tvs")
    assert not coordinator.is_listing("is the garmin 55 good", "garmin 55")


async def test_failed_product_resolution_researches_the_name_as_asked(monkeypatch):
    from types import SimpleNamespace

    from laai_agents import coordinator
    from laai_agents.models import ConsiderRequest

    async def broken(name):
        raise RuntimeError("search provider crashed")

    async def no_images(product, picks):
        return None, picks

    asked = []

    async def send_and_receive(address, request, response_type, timeout):
        asked.append(request.product)
        raise TimeoutError

    monkeypatch.setenv("PRODUCT_DATA_MODE", "live")
    monkeypatch.setattr(live, "resolve_product", broken)
    monkeypatch.setattr(coordinator, "with_images", no_images)
    monkeypatch.setattr(coordinator, "RETRY_DELAY", 0)
    addresses = {n: n for n in ("laai-price-compare", "laai-reviews", "laai-alternatives")}
    ctx = SimpleNamespace(send_and_receive=send_and_receive)
    result = await coordinator.consider(ctx, addresses, ConsiderRequest(product="garmin 55"))
    assert set(asked) == {"garmin 55"} and "price" in result.errors


# Titles exactly as Amazon listed them in a live accuracy run.
@pytest.mark.parametrize(
    "title, product, same",
    [
        ("Sony WH-1000XM5B Noise Canceling Wireless Headphones - 30hr Battery Life", "Sony WH-1000XM5", True),
        ("Sony WH-1000XM6 The Best Noise Cancelling Wireless Headphones, Black", "Sony WH-1000XM5", False),
        ("Apple AirPods Pro 3 Wireless Earbuds with Active Noise Cancellation", "AirPods Pro 2", False),
        ("Apple AirPods Pro (2nd Generation) Wireless Ear Buds with USB-C Charging", "AirPods Pro 2", True),
        ("Apple AirPods Pro (1st Generation) with MagSafe Charging Case", "AirPods Pro 2", False),
        ("Apple AirPods Pro 2nd Gen Wireless Earbuds", "AirPods Pro 2", True),
        ("Garmin Forerunner 55 GPS Running Smartwatch, Black", "Garmin Forerunner 55", True),
        ("Garmin Forerunner 935 Running GPS Unit (Black)", "Garmin Forerunner 55", False),
        ("Google Pixel 8a 128GB Unlocked", "Google Pixel 8", False),
    ],
)
def test_listing_titles_match_only_the_same_model(title, product, same):
    assert live.matches_product(title, live.product_name(product)) is same
