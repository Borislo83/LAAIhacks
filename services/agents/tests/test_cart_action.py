"""`add_to_amazon_cart`: listing pick, proposal, approval-gated add, dry run, idempotency.

The executor is always a fake here: no test may reach Amazon or open Chrome.
"""

import pytest
from conftest import REAL_PICK_LISTING
from test_g2_e2e import base_url, by_type, demo_catalog, page_body, raw  # noqa: F401

from laai_agents import amazon_cart, buyer, cart_action, stores
from laai_agents.g2_sim import G2Client
from laai_agents.integration import CART, PageWatcher
from laai_agents.models import Offer

PRODUCT = "Sony WH-1000XM5"
ASIN = "B09XS7JWHH"
TOKENS = {"web": "demo-web", "agent": "demo-agent", "executor": "demo-executor"}
LISTING = {
    "asin": ASIN,
    "url": f"https://www.amazon.com/dp/{ASIN}",
    "title": "Sony WH-1000XM5 Wireless Noise Canceling Headphones, Black",
    "price": 328.0,
    "seller": "Amazon.com",
    "condition": "new",
    "on_amazon_page": False,
}


@pytest.fixture
def api_url(base_url):  # noqa: F811
    return base_url


def offer(title, asin, price):
    return Offer(
        product=title,
        store="amazon.com",
        price=price,
        url=f"https://www.amazon.com/dp/{asin}",
        source="amazon_search_page",
        condition="used/refurb" if "renewed" in title.lower() else "new",
    )


@pytest.fixture
def amazon_search(monkeypatch):
    """Amazon's search page for the XM5: an XM4, a renewed XM5, two new XM5 listings."""

    async def page_search(query, max_price=None):
        return [
            offer("Sony WH-1000XM4 Wireless Headphones", "B0863TXGM3", 248.0),
            offer("Sony WH-1000XM5 (Renewed)", "B0BXXXXXX1", 239.0),
            offer("Sony WH-1000XM5 Wireless Headphones, Silver", "B09XS7JWHI", 349.99),
            offer(LISTING["title"], ASIN, 329.99),
        ]

    async def web_search(query):
        return [], {"data_source": "test", "sources": [], "notice": None}

    pages = {
        ASIN: (LISTING["title"], 328.0, "Amazon.com", "new"),
        "B09XS7JWHI": ("Sony WH-1000XM5 Wireless Headphones, Silver", 349.99, "Amazon.com", "new"),
        "B0BXXXXXX1": ("Sony WH-1000XM5 (Renewed)", 239.0, "Renewed Store", "used/refurb"),
    }

    async def listing_page(url, asin):
        assert url == f"https://www.amazon.com/dp/{asin}" and asin != "B0863TXGM3", (
            "only the same model's own listing pages are read"
        )
        title, price, seller, condition = pages[asin]
        return {"title": title, "price": price, "seller": seller, "condition": condition}

    monkeypatch.setattr(stores, "amazon_page_search", page_search)
    monkeypatch.setattr(buyer, "search", web_search)
    monkeypatch.setattr(cart_action, "read_listing", listing_page)
    monkeypatch.setattr(cart_action, "pick_listing", REAL_PICK_LISTING)


class FakeExecutor:
    def __init__(self, outcome=None):
        self.calls = []
        self.outcome = outcome or {"status": "cart_added", "message": "Item verified in your Amazon cart."}

    async def __call__(self, url, quantity, require_new=False):
        self.calls.append((url, quantity))
        self.require_new = require_new
        return self.outcome


@pytest.fixture
def executor(monkeypatch):
    fake = FakeExecutor()
    monkeypatch.setattr(amazon_cart, "add_to_cart", fake)
    return fake


# --- Picking the listing -----------------------------------------------------------------


async def test_pick_prefers_new_cheapest_listing_of_the_same_model(amazon_search):
    listing = await cart_action.pick_listing({"url": "https://example.com/sony"}, PRODUCT)
    # Not the XM4 (other model) nor the cheaper renewed one: the cheapest new XM5, at the
    # buy-box price and seller read from its own page ($328, not the search card's $329.99).
    assert listing == LISTING
    description = cart_action.describe(listing)
    assert f"ASIN {ASIN}" in description and "$328.00" in description
    assert "new" in description and "sold by Amazon.com" in description
    assert "Checkout is not started" in description


async def test_amazon_product_page_uses_its_own_asin(amazon_search, monkeypatch):
    async def listing_page(url, asin):
        assert url == "https://www.amazon.com/dp/B09XS7JWHI"
        return {"title": "Sony WH-1000XM5, Silver", "price": 349.99, "seller": "Amazon.com", "condition": "new"}

    monkeypatch.setattr(cart_action, "read_listing", listing_page)
    page = {"url": "https://www.amazon.com/Sony-WH-1000XM5/dp/B09XS7JWHI/ref=sr_1_1?th=1"}
    listing = await cart_action.pick_listing(page, PRODUCT)
    assert listing["asin"] == "B09XS7JWHI" and listing["price"] == 349.99
    assert listing["on_amazon_page"]


async def test_no_amazon_listing_means_no_pick(monkeypatch):
    async def web_search(query):
        return [], {"data_source": "test", "sources": [], "notice": None}

    monkeypatch.setattr(buyer, "search", web_search)
    monkeypatch.setattr(cart_action, "pick_listing", REAL_PICK_LISTING)
    assert await cart_action.pick_listing({"url": "https://example.com/x"}, PRODUCT) is None


def test_parse_listing_page_reads_buy_box_and_seller_and_checks_asin():
    page = f"""<html><body><input type="hidden" id="ASIN" value="{ASIN}">
      <input id="add-to-cart-button" type="submit">
      <span id="productTitle"> Sony WH-1000XM5 Headphones </span>
      <div id="corePriceDisplay_desktop_feature_div"><span class="a-price">
        <span class="a-offscreen">$328.00</span></span></div>
      <div id="merchantInfoFeature_feature_div"><span class="offer-display-feature-text-message">
        Amazon.com</span></div></body></html>"""
    assert cart_action.parse_listing_page(page, ASIN) == {
        "title": "Sony WH-1000XM5 Headphones",
        "price": 328.0,
        "seller": "Amazon.com",
        "condition": "new",
    }
    assert cart_action.parse_listing_page(page, "B000000000") is None
    # No Add to Cart at all: unavailable, so not a listing to propose.
    assert cart_action.parse_listing_page(page.replace('id="add-to-cart-button"', ""), ASIN) is None


def test_used_only_buy_box_is_labeled_used():
    # Amazon's page as served today for the XM5: only "Buy Used" from Amazon Resale.
    page = f"""<html><body><input type="hidden" id="ASIN" value="{ASIN}">
      <span id="productTitle">Sony WH-1000XM5 Premium Noise Cancelling Wireless Headphones, Black</span>
      <div id="corePriceDisplay_desktop_feature_div"><span class="a-price priceToPay">
        <span class="a-price-symbol">$</span><span class="a-price-whole">188<span>.</span></span>
        <span class="a-price-fraction">10</span></span></div>
      <div id="buybox"><div id="usedBuySection">Buy Used : $188.10</div>
        Used: Like New | Details Sold by <a id="sellerProfileTriggerId">Amazon Resale</a>
        <input id="add-to-cart-button-ubb" type="submit"></div></body></html>"""
    read = cart_action.parse_listing_page(page, ASIN)
    assert read["price"] == 188.10 and read["seller"] == "Amazon Resale"
    assert read["condition"] == "used/refurb (Used: Like New)"


# --- Adding (always a fake executor) -------------------------------------------------------


async def test_dry_run_never_calls_the_executor(executor):
    message = await cart_action.add_listing(LISTING)
    assert message.startswith("Dry run: would add Sony WH-1000XM5")
    assert "$328.00" in message and "Nothing was added" in message
    assert executor.calls == []


async def test_real_add_reports_the_verified_item(executor, monkeypatch):
    monkeypatch.setenv("AMAZON_CART_DRY_RUN", "0")
    message = await cart_action.add_listing(LISTING)
    assert executor.calls == [(LISTING["url"], 1)] and executor.require_new
    assert message == f"Added {LISTING['title']} ($328.00) to your Amazon cart. Checkout not started."


@pytest.mark.parametrize(
    ("outcome", "expected"),
    [
        (amazon_cart.NEEDS_SIGN_IN, "Needs Amazon sign-in"),
        ({"status": "cart_unverified", "message": "Could not verify."}, "Could not confirm"),
        ({"status": "blocked", "message": "Amazon did not show the approved ASIN."}, "Not added"),
    ],
)
async def test_failures_are_reported_honestly(monkeypatch, outcome, expected):
    monkeypatch.setenv("AMAZON_CART_DRY_RUN", "0")
    monkeypatch.setattr(amazon_cart, "add_to_cart", FakeExecutor(outcome))
    with pytest.raises(cart_action.CartError) as error:
        await cart_action.add_listing(LISTING)
    assert str(error.value).startswith(expected)
    assert "Added" not in str(error.value)


# --- Through the watcher, the real orchestrator and migration SQL ---------------------------


async def new_session(api_url):
    status, session = await raw(api_url, "demo-web", "POST", "/v1/sessions", {})
    assert status == 201
    return session["session_id"]


async def put_page(api_url, session_id, request_id, url, page_id="page-sony"):
    body = {**page_body(page_id=page_id), "url": url}
    status, _ = await raw(
        api_url,
        "demo-web",
        "PUT",
        f"/v1/sessions/{session_id}/page",
        {"schema_version": "1.0", "request_id": request_id, **body},
    )
    assert status == 200


async def settle(worker, passes=3):
    for _ in range(passes):
        await worker.watch_once()


async def test_watcher_proposes_the_named_listing_and_adds_it_once(
    api_url, tmp_path, amazon_search, executor, monkeypatch
):
    session_id = await new_session(api_url)
    worker = PageWatcher(api_url, TOKENS, tmp_path / "watch.json")
    try:
        await put_page(api_url, session_id, "cart-page-1", "https://example.com/sony-wh-1000xm5")
        async with G2Client(api_url, "demo-g2", session_id) as g2:
            await worker.watch_once()
            listed = await g2.wait_for(lambda d: d["mode"] == "actions")
            # Not an Amazon page: research first, the cart add last.
            assert [a["type"] for a in listed["actions"]] == ["compare_prices", "find_reviews", CART]
            cart = by_type(listed, CART)
            assert cart["label"] == "Add to Amazon cart"
            assert cart["description"] == cart_action.describe(LISTING)
            assert executor.calls == []  # Proposing never adds.

            # Approved on the glasses, in dry run: says what it would add, adds nothing.
            await g2.decide(cart["action_id"], request_id="cart-approve-1")
            await settle(worker)
            done = await g2.wait_for(lambda d: by_type(d, CART)["status"] == "completed")
            message = by_type(done, CART)["result"]["message"]
            assert message.startswith("Dry run: would add") and "$328.00" in message
            assert executor.calls == []
    finally:
        await worker.close()

    # A real add on an Amazon product page: the cart action comes first, runs once.
    monkeypatch.setenv("AMAZON_CART_DRY_RUN", "0")
    session_id = await new_session(api_url)
    worker = PageWatcher(api_url, TOKENS, tmp_path / "watch-real.json")
    try:
        await put_page(
            api_url, session_id, "cart-page-2", f"https://www.amazon.com/dp/{ASIN}", "page-amzn"
        )
        async with G2Client(api_url, "demo-g2", session_id) as g2:
            await worker.watch_once()
            listed = await g2.wait_for(lambda d: d["mode"] == "actions")
            assert listed["actions"][0]["type"] == CART
            cart = listed["actions"][0]
            assert f"ASIN {ASIN}" in cart["description"]
            await g2.decide(cart["action_id"], request_id="cart-approve-2")
            await settle(worker)
            done = await g2.wait_for(lambda d: by_type(d, CART)["status"] == "completed")
            assert by_type(done, CART)["result"]["message"] == (
                f"Added {LISTING['title']} ($328.00) to your Amazon cart. Checkout not started."
            )
            assert executor.calls == [(LISTING["url"], 1)]

            # The approval delivered again (cursor rewound): its journaled outcome is
            # re-posted, nothing is added twice.
            state = worker.state["sessions"][session_id]
            state["cursor"] = ""
            await worker.workflow(session_id).poll_once()
            assert executor.calls == [(LISTING["url"], 1)]
    finally:
        await worker.close()


async def test_watcher_skips_the_cart_action_without_an_amazon_listing(api_url, tmp_path):
    # conftest's pick_listing finds nothing: only the research actions are proposed.
    session_id = await new_session(api_url)
    worker = PageWatcher(api_url, TOKENS, tmp_path / "watch.json")
    try:
        await put_page(api_url, session_id, "nocart-page", "https://example.com/sony")
        await settle(worker)
        _, shown = await raw(
            api_url, "demo-g2", "GET", f"/v1/sessions/{session_id}/display",
            params={"after_revision": "0"},
        )
        assert [a["type"] for a in shown["actions"]] == ["compare_prices", "find_reviews"]
    finally:
        await worker.close()


async def test_api_without_the_cart_type_still_gets_the_research_actions(
    api_url, tmp_path, monkeypatch
):
    async def listing(page, product):
        return LISTING

    monkeypatch.setattr(cart_action, "pick_listing", listing)
    session_id = await new_session(api_url)
    worker = PageWatcher(api_url, TOKENS, tmp_path / "watch.json")
    real_api = worker.api

    async def old_api(role, method, path, body=None, params=None):
        # An API before migration 007 refuses the whole proposal over the cart type.
        if method == "PUT" and path.endswith("/actions") and any(
            a["type"] == CART for a in body["actions"]
        ):
            from laai_agents.integration import GlassesAPIError

            raise GlassesAPIError(method, 422)
        return await real_api(role, method, path, body, params)

    worker.api = old_api
    try:
        await put_page(api_url, session_id, "old-api-page", "https://example.com/sony")
        await settle(worker)
        _, shown = await raw(
            api_url, "demo-g2", "GET", f"/v1/sessions/{session_id}/display",
            params={"after_revision": "0"},
        )
        assert [a["type"] for a in shown["actions"]] == ["compare_prices", "find_reviews"]
        assert worker.cart_supported is False
    finally:
        await worker.close()


async def test_an_add_already_started_is_never_repeated(tmp_path, executor, monkeypatch):
    # A crash after the add began and before its outcome was journaled: the redelivered
    # approval reports it instead of adding again.
    monkeypatch.setenv("AMAZON_CART_DRY_RUN", "0")
    worker = PageWatcher("http://localhost:1", TOKENS, tmp_path / "watch.json")
    plan = {"handler": CART, "listing": LISTING, "product": PRODUCT}
    first = await worker.run_plan(plan)
    assert first["status"] == "completed" and plan["cart_started"]
    again = await worker.run_plan(plan)
    assert again["status"] == "failed" and "already attempted" in again["message"]
    assert executor.calls == [(LISTING["url"], 1)]


async def test_web_search_listings_are_priced_from_their_own_pages(monkeypatch):
    # Amazon's search page lists no XM5; web search finds ASINs without prices. Their own
    # pages supply the price, and the cheapest new one is named.
    async def web_search(query):
        from laai_agents.models import Source

        return [
            Source(title="Amazon.com: Sony WH-1000XM5 Premium Noise Cancelling", url=f"https://www.amazon.com/dp/{ASIN}"),
            Source(title="Sony WH-1000XM5 Wireless Headphones", url="https://www.amazon.com/dp/B09Z2S9FSX"),
        ], {"data_source": "test", "sources": [], "notice": None}

    async def listing_page(url, asin):
        price = {ASIN: 328.0, "B09Z2S9FSX": 299.0}[asin]
        return {"title": f"Sony WH-1000XM5 {asin}", "price": price, "seller": "Amazon.com", "condition": "new"}

    monkeypatch.setattr(buyer, "search", web_search)
    monkeypatch.setattr(cart_action, "read_listing", listing_page)
    monkeypatch.setattr(cart_action, "pick_listing", REAL_PICK_LISTING)
    listing = await cart_action.pick_listing({"url": "https://example.com/x"}, PRODUCT)
    assert listing["asin"] == "B09Z2S9FSX" and listing["price"] == 299.0
    assert listing["seller"] == "Amazon.com"


async def test_dry_run_is_on_unless_explicitly_disabled(executor, monkeypatch):
    # conftest sets AMAZON_CART_DRY_RUN=1 for every test; the default itself must be on.
    monkeypatch.delenv("AMAZON_CART_DRY_RUN")
    assert cart_action.dry_run()
    message = await cart_action.add_listing(LISTING)
    assert message.startswith("Dry run:") and executor.calls == []
    for value in ("1", "", "true", "yes"):
        monkeypatch.setenv("AMAZON_CART_DRY_RUN", value)
        assert cart_action.dry_run()
    monkeypatch.setenv("AMAZON_CART_DRY_RUN", "0")
    assert not cart_action.dry_run()


async def test_the_agents_cart_endpoint_needs_the_executor_token_and_a_real_listing(monkeypatch, executor):
    from laai_agents.buyer import CartListingRequest, add_approved_listing

    monkeypatch.setenv("AMAZON_CART_DRY_RUN", "0")
    monkeypatch.setenv("AMAZON_CART_EXECUTOR_TOKEN", "t" * 32)
    wrong = await add_approved_listing(CartListingRequest(listing=LISTING, executor_token="nope"))
    forged = await add_approved_listing(
        CartListingRequest(listing={**LISTING, "url": "https://evil.example/dp/x"}, executor_token="t" * 32)
    )
    assert (wrong.status, forged.status) == ("not_connected", "refused")
    assert executor.calls == []
    added = await add_approved_listing(CartListingRequest(listing=LISTING, executor_token="t" * 32))
    assert added.status == "cart_added" and executor.calls == [(LISTING["url"], 1)]


async def test_without_the_agents_running_the_watcher_adds_in_process(monkeypatch, executor):
    monkeypatch.setenv("AMAZON_CART_DRY_RUN", "0")
    message = await cart_action.add_listing_on_agents(LISTING)
    assert message.startswith("Added") and executor.calls == [(LISTING["url"], 1)]
