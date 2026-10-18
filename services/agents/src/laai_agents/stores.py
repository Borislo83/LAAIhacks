"""Store listings from official or licensed APIs: real products, prices and cart links.

- Best Buy Products API (BESTBUY_API_KEY, free at developer.bestbuy.com)
- SerpApi Amazon and Google Shopping engines (SERPAPI_API_KEY, serpapi.com)

Each source returns nothing when its key is missing or the call fails, so research
falls back to web evidence. Listings are filtered to the budget before returning.
"""

import asyncio
import os
import re
import time

import aiohttp

from .carts import amazon_cart_link, bestbuy_cart_link
from .models import Offer

TIMEOUT = aiohttp.ClientTimeout(total=12)
BESTBUY_FIELDS = (
    "sku,name,salePrice,regularPrice,url,addToCartUrl,"
    "customerReviewAverage,customerReviewCount,onlineAvailability"
)


def has_store_keys() -> bool:
    return bool(os.getenv("BESTBUY_API_KEY") or os.getenv("SERPAPI_API_KEY"))


def _words(query: str) -> list[str]:
    return [w for w in re.findall(r"[A-Za-z0-9]+", query) if len(w) > 1][:6]


def _price(value) -> float | None:
    try:
        price = float(str(value).replace("$", "").replace(",", ""))
    except (TypeError, ValueError):
        return None
    return price if price > 0 else None


def _offer(product, store, price, url, cart_url, rating, reviews, source):
    return Offer(
        product=product,
        store=store,
        price=price,
        url=url,
        source=source,
        trust="high",  # official/licensed listing data
        cart_url=cart_url,
        rating=round(float(rating), 1) if rating not in (None, "") else None,
        review_count=int(reviews) if str(reviews or "").isdigit() else None,
    )


# SerpApi's free tier is ~100 searches a month, so identical lookups within half an
# hour are answered from memory. Keys are left out of the cache key.
_CACHE: dict[tuple, tuple[float, dict]] = {}
CACHE_SECONDS = 1800


async def _get_json(url: str, params: dict) -> dict:
    key = (url, tuple(sorted((k, str(v)) for k, v in params.items() if "key" not in k.lower())))
    cached = _CACHE.get(key)
    if cached and time.monotonic() - cached[0] < CACHE_SECONDS:
        return cached[1]
    data = await _fetch_json(url, params)
    if data:
        _CACHE[key] = (time.monotonic(), data)
    return data


async def _fetch_json(url: str, params: dict) -> dict:
    async with (
        aiohttp.ClientSession(timeout=TIMEOUT) as session,
        session.get(url, params=params) as response,
    ):
        if response.status != 200:
            return {}
        return await response.json(content_type=None)


async def bestbuy_search(query: str, max_price: float | None = None) -> list[Offer]:
    key = os.getenv("BESTBUY_API_KEY", "")
    words = _words(query)
    if not key or not words:
        return []
    terms = "&".join(f"search={w}" for w in words)
    if max_price is not None:
        terms += f"&salePrice<{max_price:.2f}"
    try:
        data = await _get_json(
            f"https://api.bestbuy.com/v1/products({terms})",
            {
                "apiKey": key,
                "format": "json",
                "show": BESTBUY_FIELDS,
                "pageSize": 10,
                "sort": "customerReviewCount.dsc",
            },
        )
    except (TimeoutError, aiohttp.ClientError, ValueError):
        return []
    offers = []
    for item in data.get("products", []):
        price = _price(item.get("salePrice"))
        if price is None or item.get("onlineAvailability") is False:
            continue
        sku = item.get("sku")
        offers.append(
            _offer(
                str(item.get("name", "")),
                "bestbuy.com",
                price,
                str(item.get("url") or f"https://www.bestbuy.com/site/{sku}.p"),
                bestbuy_cart_link(sku),
                item.get("customerReviewAverage"),
                item.get("customerReviewCount"),
                "bestbuy_api",
            )
        )
    return offers


async def amazon_search(query: str, max_price: float | None = None) -> list[Offer]:
    key = os.getenv("SERPAPI_API_KEY", "")
    if not key or not _words(query):
        return []
    try:
        data = await _get_json(
            "https://serpapi.com/search.json",
            {"engine": "amazon", "k": query, "amazon_domain": "amazon.com", "api_key": key},
        )
    except (TimeoutError, aiohttp.ClientError, ValueError):
        return []
    offers = []
    for item in data.get("organic_results", [])[:15]:
        asin = str(item.get("asin", ""))
        price = _price(item.get("extracted_price"))
        cart = amazon_cart_link(asin)
        if price is None or cart is None:
            continue
        offers.append(
            _offer(
                str(item.get("title", "")),
                "amazon.com",
                price,
                f"https://www.amazon.com/dp/{asin.upper()}",
                cart,
                item.get("rating"),
                item.get("reviews"),
                "serpapi_amazon",
            )
        )
    return offers


async def shopping_search(query: str, max_price: float | None = None) -> list[Offer]:
    """Other stores via Google Shopping: shown with their store, no cart button."""
    key = os.getenv("SERPAPI_API_KEY", "")
    if not key or not _words(query):
        return []
    try:
        data = await _get_json(
            "https://serpapi.com/search.json",
            {"engine": "google_shopping", "q": query, "gl": "us", "hl": "en", "api_key": key},
        )
    except (TimeoutError, aiohttp.ClientError, ValueError):
        return []
    offers = []
    for item in data.get("shopping_results", [])[:15]:
        price = _price(item.get("extracted_price"))
        url = str(item.get("product_link") or item.get("link") or "")
        store = str(item.get("source", "")).strip()
        # Amazon and Best Buy come from their own sources above, with cart links.
        if price is None or not url.startswith("https://") or not store:
            continue
        if re.search(r"amazon|best\s*buy", store, re.IGNORECASE):
            continue
        offers.append(
            _offer(
                str(item.get("title", "")),
                store,
                price,
                url,
                None,
                item.get("rating"),
                item.get("reviews"),
                "serpapi_shopping",
            )
        )
    return offers


async def store_offers(query: str, max_price: float | None = None) -> list[Offer]:
    """Listings from every configured store API, within budget."""
    results = await asyncio.gather(
        bestbuy_search(query, max_price),
        amazon_search(query, max_price),
        shopping_search(query, max_price),
    )
    offers = [o for group in results for o in group]
    if max_price is not None:
        offers = [o for o in offers if o.price < max_price]
    return offers
