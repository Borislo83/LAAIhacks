import pytest

from laai_agents import stores

# Without a SerpApi key, Amazon listings come from Amazon's public search page. Tests
# never reach it; a test that needs the reader calls REAL_AMAZON_PAGE_SEARCH directly.
REAL_AMAZON_PAGE_SEARCH = stores.amazon_page_search


@pytest.fixture(autouse=True)
def offline_amazon_page(monkeypatch):
    async def nothing(query, max_price=None):
        return []

    monkeypatch.setattr(stores, "amazon_page_search", nothing)
