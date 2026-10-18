from types import SimpleNamespace

from laai_agents import amazon_cart

URL = "https://www.amazon.com/dp/B0B5VHRX7F"
CART = "https://www.amazon.com/gp/cart/view.html"


class Locator:
    def __init__(self, page, selector):
        self.page, self.selector = page, selector

    @property
    def first(self):
        return self

    def nth(self, index):
        return self

    async def count(self):
        if "data-asin" in self.selector:
            return int(self.page.added)
        return int(self.selector in {"#ASIN", "#add-to-cart-button:visible"})

    async def is_visible(self):
        return bool(await self.count())

    async def is_enabled(self):
        return True

    async def input_value(self):
        return self.page.asin

    async def click(self, **_):
        self.page.clicks.append(self.selector)
        self.page.added = self.page.verify_add

    async def wait_for(self, **_):
        from playwright.async_api import TimeoutError

        if not self.page.added:
            raise TimeoutError("Item not found")


class Page:
    def __init__(self, verify_add=True, asin="B0B5VHRX7F", added=False):
        self.url, self.asin = CART, asin
        self.added, self.verify_add = added, verify_add
        self.clicks, self.visits = [], []

    async def goto(self, url, **_):
        self.url = url
        self.visits.append(url)

    def locator(self, selector):
        return Locator(self, selector)

    def is_closed(self):
        return False

    async def wait_for_selector(self, *_, **__):
        pass


def browser_fixture(monkeypatch, page):
    opened = []

    async def new_page():
        opened.append(page)
        return page

    monkeypatch.setattr(amazon_cart, "_playwright", object())
    monkeypatch.setattr(amazon_cart, "_page", None)
    monkeypatch.setattr(
        amazon_cart, "_browser", SimpleNamespace(pages=[page], new_page=new_page)
    )
    return opened


async def test_adds_only_exact_asin_and_stops_in_verified_cart(monkeypatch):
    page = Page()
    browser_fixture(monkeypatch, page)
    result = await amazon_cart.add_to_cart(URL, 1)
    assert result["status"] == "cart_added"
    assert page.clicks == ["#add-to-cart-button:visible"]
    assert page.visits == [CART, URL, CART]


async def test_wrong_variant_is_never_added(monkeypatch):
    page = Page(asin="B000000000")
    browser_fixture(monkeypatch, page)
    assert (await amazon_cart.add_to_cart(URL, 1))["status"] == "blocked"
    assert page.clicks == []


async def test_missing_cart_evidence_never_claims_success(monkeypatch):
    page = Page(verify_add=False)
    browser_fixture(monkeypatch, page)
    assert (await amazon_cart.add_to_cart(URL, 1))["status"] == "cart_unverified"
    assert page.clicks == ["#add-to-cart-button:visible"]


async def test_existing_cart_item_does_not_get_duplicated(monkeypatch):
    page = Page(added=True)
    browser_fixture(monkeypatch, page)
    assert (await amazon_cart.add_to_cart(URL, 1))["status"] == "cart_added"
    assert page.clicks == [] and page.visits == [CART]


async def test_amazon_duplicate_asin_fields_and_ubb_add_button(monkeypatch):
    class CurrentLocator(Locator):
        async def count(self):
            if self.selector == "#ASIN":
                return 3
            if self.selector == "#add-to-cart-button:visible":
                return 0
            if self.selector == "#add-to-cart-button-ubb:visible":
                return 1
            return await super().count()

    class CurrentPage(Page):
        def locator(self, selector):
            return CurrentLocator(self, selector)

    page = CurrentPage()
    browser_fixture(monkeypatch, page)
    assert (await amazon_cart.add_to_cart(URL, 1))["status"] == "cart_added"
    assert page.clicks == ["#add-to-cart-button-ubb:visible"]


async def test_repeated_approvals_reuse_one_tab(monkeypatch):
    page = Page(added=True)
    opened = browser_fixture(monkeypatch, page)
    for _ in range(3):
        assert (await amazon_cart.add_to_cart(URL, 1))["status"] == "cart_added"
    assert len(opened) == 1
