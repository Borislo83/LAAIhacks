import asyncio

import pytest

from laai_agents import cart_action, live, merchants, stores

# Without a SerpApi key, Amazon listings come from Amazon's public search page. Tests
# never reach it; a test that needs the reader calls REAL_AMAZON_PAGE_SEARCH directly.
REAL_AMAZON_PAGE_SEARCH = stores.amazon_page_search
REAL_WALMART_PAGE_SEARCH = stores.walmart_page_search
# The watcher's cart listing pick; tests of it call this (see test_cart_action).
REAL_PICK_LISTING = cart_action.pick_listing


@pytest.fixture(autouse=True)
def offline_amazon_page(monkeypatch):
    async def nothing(query, max_price=None):
        return []

    monkeypatch.setattr(stores, "amazon_page_search", nothing)
    # Walmart's search page likewise; tests parse saved fixture HTML instead.
    monkeypatch.setattr(stores, "walmart_page_search", nothing)
    monkeypatch.setattr(stores, "_WALMART", {})
    # No answers carried over between tests: price/alternatives caches, blocked stores.
    monkeypatch.setattr(live, "_PRICE_CACHE", {})
    monkeypatch.setattr(live, "_ALT_CACHE", {})
    monkeypatch.setattr(merchants, "_BLOCKED", {})
    # Each test reads its own pages: no cached answers, a fresh one-at-a-time slot.
    monkeypatch.setattr(stores, "_PAGES", {})
    monkeypatch.setattr(stores, "_AMAZON_SLOT", asyncio.Semaphore(1))

    # The cart action: dry run, and no listing unless a test supplies one.
    monkeypatch.setenv("AMAZON_CART_DRY_RUN", "1")
    monkeypatch.setattr(cart_action, "_picks", {})

    async def no_listing(page, product):
        return None

    monkeypatch.setattr(cart_action, "pick_listing", no_listing)
