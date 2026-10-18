"""Reviews give advice from the reviews' text, not just structured ratings."""

import pytest
from laai_agents import live, merchants
from laai_agents.expert_reviews import drop_contradictions, text_review
from laai_agents.models import ReviewsRequest, Source

PAGE = """<html><title>Garmin Forerunner 265 review</title><body><h1>Garmin Forerunner 265 review</h1>
<p>The Forerunner 265 is a running watch with an AMOLED screen and strong training tools.</p>
<div><strong>Pros</strong><ul><li>Bright AMOLED display</li><li>Accurate GPS</li></ul>
<strong>Cons</strong><ul><li>Pricey</li><li>Plastic build</li></ul></div>
<p>Our rating: 4.5 out of 5</p>
<p>NOTE: This review is part of our best running watch guide, which we update often.</p>
<p>If you take a nap, that shows up in a few different places related to sleep.</p>
<h2>Verdict: should you buy it?</h2>
<p>If you are a serious runner, the Forerunner 265 is worth the price for its accurate GPS. However, casual users may find cheaper watches offer better value.</p>
<h2>Specs</h2><p>It weighs 47 grams.</p></body></html>"""


@pytest.mark.parametrize(
    "question, product",
    [
        ("Is the garmin watch forerunner worth it or not", "garmin watch forerunner"),
        ("Should I buy the AirPods Pro 2?", "AirPods Pro 2"),
        ("are Beats Studio Pro any good", "Beats Studio Pro"),
        ("What do you think of the Kindle Paperwhite", "Kindle Paperwhite"),
        ("Sony headphones", "Sony headphones"),
    ],
)
def test_product_is_taken_out_of_the_question(question, product):
    assert live.clean_query(question) == product


def test_text_review_reads_score_lists_and_verdict():
    score, pros, cons, verdict = text_review(PAGE, "https://www.example.com/fr265", "garmin watch forerunner 265")
    assert score.rating == 4.5
    assert pros == ["Bright AMOLED display", "Accurate GPS"] and cons == ["Pricey", "Plastic build"]
    assert verdict[0].startswith("If you are a serious runner")
    assert not any("NOTE" in s or "nap" in s for s in verdict)  # asides and off-topic text


def test_text_review_ignores_pages_about_other_products():
    assert text_review(PAGE, "https://www.example.com/x", "Apple Watch Ultra")[3] == []


def test_cons_that_contradict_pros_are_dropped():
    assert drop_contradictions(["Waterproof design"], ["Not waterproof", "Ads on the lock screen"]) == [
        "Ads on the lock screen"
    ]


async def test_reviews_give_advice_without_structured_ratings(monkeypatch):
    async def search(query):
        sources = [Source(title="Garmin Forerunner 265 review", url="https://www.example.com/fr265")]
        return sources, {"sources": sources, "data_source": "ddgs", "notice": "n"}

    async def fetch_page(url):
        return PAGE

    monkeypatch.setattr(live, "search", search)
    monkeypatch.setattr(merchants, "fetch_page", fetch_page)
    result = await live.reviews(ReviewsRequest(product="Is the Garmin Forerunner 265 worth it?"))
    assert result.product == "Garmin Forerunner 265"
    assert result.rating == 4.5 and result.read_from == ["example.com"]
    assert "serious runner" in result.verdict and "serious runner" in result.summary
    assert result.pros and result.cons


VS_TITLES = [
    "Garmin Forerunner 55 vs Coros Pace 3: Which is better?",
    "Garmin Forerunner 55 vs Forerunner 165 - DC Rainmaker",
    "Coros Pace 3 vs Garmin Forerunner 55 review",
    "Garmin Forerunner 55 vs Polar Pacer (2025)",
    "Forerunner 55 versus Coros Pace 3 compared",
    "Best running watches 2026",
]


@pytest.mark.parametrize(
    "question, product",
    [
        ("Is there a better alteranate for the garmin 5 forerunning watch", "garmin 5 forerunning watch"),
        ("is there any better alternative to the garmin 5 forerunner watch?", "garmin 5 forerunner watch"),
        ("any cheaper options than the AirPods Pro 2", "AirPods Pro 2"),
        ("best alternatives to Sony XM5 under $300", "Sony XM5"),
        ("alter the font size", "alter the font size"),
    ],
)
def test_alternative_questions_name_the_product(question, product):
    assert live.clean_query(question) == product


def test_versus_headlines_name_the_product_and_its_rivals():
    sources = [Source(title=t, url=f"https://x.example/{i}") for i, t in enumerate(VS_TITLES)]
    canonical, rivals = live.versus_titles(sources, "garmin 5 forerunning watch")
    assert canonical == "Garmin Forerunner 55"
    assert [r for r, _ in rivals][:2] == ["Coros Pace 3", "Forerunner 165"]  # different model kept
    assert all("55" not in r for r, _ in rivals)  # the product itself is not its own rival


async def test_alternatives_for_products_outside_the_catalog(monkeypatch):
    from laai_agents.models import AlternativesRequest, PriceCompareResponse

    async def search(query):
        sources = [Source(title=t, url=f"https://x.example/{i}") for i, t in enumerate(VS_TITLES)]
        return sources, {"sources": sources, "data_source": "ddgs", "notice": "n"}

    async def no_price(req):
        return PriceCompareResponse(product=req.product, offers=[], summary="none")

    monkeypatch.setattr(live, "search", search)
    monkeypatch.setattr(live, "compare", no_price)
    assert await live.resolve_product("garmin 5 forerunning watch") == "Garmin Forerunner 55"
    result = await live.alternatives(AlternativesRequest(product="Garmin Forerunner 55"))
    assert [a.product for a in result.alternatives][:2] == ["Coros Pace 3", "Forerunner 165"]
    assert "compared with" in result.alternatives[0].reason


def test_close_spellings_match_but_model_numbers_do_not():
    from laai_agents.live import matches_product, names_product

    assert matches_product("Garmin Forerunner 55 GPS Running Watch", "Garmin Forerunning")
    assert names_product("Garmin Forerunner 265 review", "Garmin Forerunning")
    assert not matches_product("Sony WH-1000XM6 Headphones", "Sony WH-1000XM5")
    assert not matches_product("Garmin Fenix 8", "Garmin Forerunning")


def test_accessories_are_not_the_product():
    from laai_agents.merchants import is_accessory

    assert is_accessory("ESR for AirPods Pro 2 Case, Compatible with MagSafe, Black", "Apple AirPods Pro 2")
    assert is_accessory("Band Compatible with Garmin Forerunner 55, Silicone", "Garmin Forerunner 55")
    assert is_accessory("Case for AirPods Pro 2", "earbuds")
    assert not is_accessory("Apple AirPods Pro 2 Wireless Earbuds, Active Noise Cancellation", "Apple AirPods Pro 2")
    assert not is_accessory("Wireless Earbuds for iPhone with Charging Case, 40H Playtime", "earbuds")
    assert not is_accessory("Garmin Forerunner 55, GPS Running Watch", "Garmin Forerunner 55")


def test_headline_titles_are_not_products():
    from laai_agents.live import versus_titles
    from laai_agents.models import Source

    titles = [
        "Battle of the Buds: Galaxy Buds2 vs Pixel Buds Pro",
        "Galaxy Buds2 vs Galaxy Buds2 Pro: which should you buy?",
        "Samsung Galaxy Buds2 vs Nothing Ear (a) review",
    ]
    sources = [Source(title=t, url=f"https://example.com/{i}") for i, t in enumerate(titles)]
    canonical, rivals = versus_titles(sources, "Galaxy Buds2")
    names = [r for r, _ in rivals]
    assert "Battle of the Buds" not in names and "Pixel Buds Pro" in names


def test_a_model_number_inside_the_name_is_another_product():
    from laai_agents.live import matches_product

    assert not matches_product("Samsung Galaxy Buds 3 Pro AI True Wireless Earbuds", "Galaxy Buds Pro")
    assert not matches_product("Samsung Galaxy Buds3 Pro (2024, ANC)", "Galaxy Buds Pro")
    assert matches_product("Samsung Galaxy Buds Pro True Wireless Earbuds", "Galaxy Buds Pro")
    assert matches_product("Garmin Forerunner 55, GPS Running Watch", "Garmin Forerunning")
