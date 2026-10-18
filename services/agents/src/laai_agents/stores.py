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

from . import net
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


def _offer(product, store, price, url, cart_url, rating, reviews, source, image=None):
    from .merchants import condition_of

    return Offer(
        condition=condition_of(product),
        product=product,
        store=store,
        price=price,
        url=url,
        source=source,
        trust="high",  # official/licensed listing data
        cart_url=cart_url,
        rating=round(float(rating), 1) if rating not in (None, "") else None,
        review_count=int(reviews) if str(reviews or "").isdigit() else None,
        image=store_image(image),
    )


IMAGE_HOSTS = (".media-amazon.com", ".ssl-images-amazon.com", ".bbystatic.com")


def store_image(url) -> str | None:
    """A product photo URL, only from the stores' own image CDNs."""
    from urllib.parse import urlsplit

    try:
        parts = urlsplit(str(url or ""))
    except ValueError:
        return None
    host = parts.hostname or ""
    ok = parts.scheme == "https" and not parts.username and host.endswith(IMAGE_HOSTS)
    return str(url) if ok and len(str(url)) <= 500 else None


# SerpApi's free tier is ~100 searches a month, so identical lookups within half an
# hour are answered from memory. Keys are left out of the cache key.
_CACHE: dict[tuple, tuple[float, dict]] = {}
CACHE_SECONDS = 1800
CACHE_SIZE = 256


async def _get_json(url: str, params: dict) -> dict:
    key = (url, tuple(sorted((k, str(v)) for k, v in params.items() if "key" not in k.lower())))
    now = time.monotonic()
    cached = _CACHE.get(key)
    if cached and now - cached[0] < CACHE_SECONDS:
        return cached[1]
    data = await _fetch_json(url, params)
    if data:
        for stale in [k for k, (at, _) in _CACHE.items() if now - at >= CACHE_SECONDS]:
            del _CACHE[stale]
        while len(_CACHE) >= CACHE_SIZE:
            del _CACHE[next(iter(_CACHE))]  # oldest first: insertion order
        _CACHE[key] = (time.monotonic(), data)
    return data


async def _fetch_json(url: str, params: dict) -> dict:
    async with net.session().get(url, params=params, timeout=TIMEOUT) as response:
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
    if not _words(query):
        return []
    if not key:
        return await amazon_page_search(query, max_price)
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
                item.get("thumbnail"),
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
    """Listings from every configured store API and the public store search pages,
    within budget. A store that fails or is slow adds nothing; the others still count."""
    results = await asyncio.gather(
        bestbuy_search(query, max_price),
        amazon_search(query, max_price),
        shopping_search(query, max_price),
        walmart_page_search(query, max_price),
        return_exceptions=True,
    )
    for group in results:
        if isinstance(group, BaseException) and not isinstance(
            group, (TimeoutError, aiohttp.ClientError, ValueError)
        ):
            raise group
    offers = [o for group in results if not isinstance(group, BaseException) for o in group]
    if max_price is not None:
        offers = [o for o in offers if o.price < max_price]
    return offers


# --- Amazon's own search page (no key) ------------------------------------------------
# Without SerpApi, Amazon's public search results page still lists real products with
# their price, star rating and rating count. It is read once with the safe page fetcher;
# sponsored cards are skipped and a CAPTCHA page yields nothing.
AMAZON_SEARCH = "https://www.amazon.com/s"
STARS = re.compile(r"(\d(?:\.\d)?) out of 5 stars")
AMAZON_ATTEMPTS = 3
AMAZON_RETRY_DELAY = 1.0


def amazon_search_url(query: str, max_price: float | None = None) -> str:
    from urllib.parse import urlencode

    params = {"k": " ".join(query.split())[:120]}
    if max_price is not None:
        # p_36 is Amazon's price filter, in cents ("-5000" is up to $50).
        params["rh"] = f"p_36:-{int(max_price * 100)}"
    return f"{AMAZON_SEARCH}?{urlencode(params)}"


def parse_amazon_results(page: str, max_price: float | None = None) -> list[Offer]:
    from lxml import html

    from .merchants import MALFORMED

    try:
        doc = html.fromstring(page)
    except MALFORMED:
        return []
    offers, seen = [], set()
    for card in doc.xpath('//div[@data-component-type="s-search-result"]'):
        asin = (card.get("data-asin") or "").upper()
        if not asin or asin in seen or card.xpath('.//*[normalize-space(text())="Sponsored"]'):
            continue
        title = " ".join(" ".join(card.xpath(".//h2//text()")).split())
        price = _price(
            next(
                iter(
                    card.xpath(
                        './/span[contains(@class,"a-price") and not(contains(@class,"a-text-price"))]'
                        '/span[@class="a-offscreen"]/text()'
                    )
                ),
                None,
            )
        )
        cart = amazon_cart_link(asin)
        if not title or price is None or cart is None:
            continue
        if max_price is not None and price >= max_price:
            continue
        stars = STARS.search(" ".join(card.xpath('.//span[contains(@class,"a-icon-alt")]/text()')))
        counted = re.search(
            # "4.2 out of 5 stars, rating details" sits next to "4,961 ratings".
            r"\b(\d[\d,]*)\s+ratings?\b",
            " ".join(card.xpath('.//*[contains(@aria-label,"rating")]/@aria-label')),
        )
        seen.add(asin)
        image = next(iter(card.xpath('.//img[contains(@class,"s-image")]/@src')), None)
        offers.append(
            _offer(
                title[:200],
                "amazon.com",
                price,
                f"https://www.amazon.com/dp/{asin}",
                cart,
                stars[1] if stars else None,
                counted[1].replace(",", "") if counted else None,
                "amazon_search_page",
                image,
            )
        )
    return offers


# Amazon throttles bursts, so its page is read one request at a time and each answer is
# reused for half an hour (price, alternatives and photos often ask for the same thing).
_PAGES: dict[tuple, tuple[float, list[Offer]]] = {}
_AMAZON_SLOT = asyncio.Semaphore(1)


async def amazon_page_search(query: str, max_price: float | None = None) -> list[Offer]:
    if not _words(query):
        return []
    key = (" ".join(query.lower().split()), max_price)
    cached = _PAGES.get(key)
    if cached and time.monotonic() - cached[0] < CACHE_SECONDS:
        return cached[1]
    async with _AMAZON_SLOT:
        cached = _PAGES.get(key)
        if cached and time.monotonic() - cached[0] < CACHE_SECONDS:
            return cached[1]
        offers = await _read_amazon_page(query, max_price)
    if offers:
        while len(_PAGES) >= CACHE_SIZE:
            del _PAGES[next(iter(_PAGES))]
        _PAGES[key] = (time.monotonic(), offers)
    return offers


async def product_image(name: str) -> str | None:
    """The photo on the Amazon listing that names this product."""
    # New listings first: a "Renewed" one shows the same product, but used. (A sorted
    # copy: the listings are cached and shared with the price agent.)
    offers = sorted(await amazon_page_search(name), key=lambda o: "renewed" in o.product.lower())
    for offer in offers:
        if offer.image and photo_of(offer.product, name):
            return offer.image
    return None


def photo_of(title: str, name: str) -> bool:
    """The listing shows this product. Amazon writes models many ways ("WH-1000XM5/B",
    "WH1000XM5"), so the model is compared without spaces or punctuation."""
    from .live import names_product

    if names_product(title, name):
        return True
    compact = re.sub(r"[^a-z0-9]", "", title.lower())
    words = re.findall(r"[a-z0-9]+", name.lower())
    # The name without its brand ("WH-1000XM5" for "Sony WH-1000XM5"), or all of it.
    for part in ("".join(words[1:]), "".join(words)):
        if len(part) >= 5 and part in compact:
            return True
    return False


async def _read_amazon_page(query: str, max_price: float | None) -> list[Offer]:
    from .merchants import fetch_page

    # Amazon sometimes answers with a short interstitial instead of results, so it gets
    # three attempts like the agents do.
    for attempt in range(AMAZON_ATTEMPTS):
        if attempt:
            await asyncio.sleep(AMAZON_RETRY_DELAY * attempt)
        page = await fetch_page(
            amazon_search_url(query, max_price), limit=4_000_000,
            timeout=aiohttp.ClientTimeout(total=3.5),
        )
        if not page or "captcha" in page[:20000].lower():
            continue
        offers = parse_amazon_results(page, max_price)
        if offers or 's-search-result' in page:
            return offers
    return []


# --- Walmart's own search page (no key) -----------------------------------------------
# Walmart's public search page embeds its results as JSON (__NEXT_DATA__): name, current
# price, seller and condition. Items sold by Walmart.com are high trust; marketplace
# sellers are medium and named; restored/open-box/pre-owned items are labeled used.
WALMART_SEARCH = "https://www.walmart.com/search"
WALMART_CONDITION_NEW = 1


def walmart_search_url(query: str) -> str:
    from urllib.parse import urlencode

    return f"{WALMART_SEARCH}?{urlencode({'q': ' '.join(query.split())[:120]})}"


def _walmart_items(node):
    if isinstance(node, dict):
        if "priceInfo" in node and "name" in node and node.get("canonicalUrl"):
            yield node
            return
        for value in node.values():
            yield from _walmart_items(value)
    elif isinstance(node, list):
        for value in node:
            yield from _walmart_items(value)


def _walmart_price(item: dict) -> float | None:
    details = (item.get("priceInfo") or {}).get("priceDetails") or {}
    if details.get("currency", "USD") != "USD":
        return None
    for line in details.get("priceLines") or []:
        if line.get("lineType") in {"CURRENT_PRICE", "DISCOUNTED_PRICE"}:
            for value in line.get("values") or []:
                if value.get("key") == "PRICE":
                    return _price(value.get("value"))
    # Older pages carry only the number.
    price = item.get("price")
    return _price(price) if isinstance(price, (int, float)) else None


def parse_walmart_results(page: str, max_price: float | None = None) -> list[Offer]:
    import json

    from lxml import html

    from .merchants import MALFORMED, USED

    try:
        doc = html.fromstring(page)
        data = json.loads(doc.xpath('//script[@id="__NEXT_DATA__"]/text()')[0])
    except (*MALFORMED, IndexError, TypeError):
        return []
    offers, seen = [], set()
    for item in _walmart_items(data):
        path = str(item.get("canonicalUrl", "")).split("?")[0]
        name = " ".join(str(item.get("name", "")).split())
        price = _walmart_price(item)
        if not path.startswith("/ip/") or path in seen or not name or price is None:
            continue
        if item.get("isOutOfStock"):
            continue
        if max_price is not None and price >= max_price:
            continue
        seen.add(path)
        code = (item.get("conditionV2") or {}).get("code", WALMART_CONDITION_NEW)
        used = item.get("isPreowned") or code != WALMART_CONDITION_NEW or USED.search(name)
        seller = " ".join(str(item.get("sellerName") or "").split())[:80] or None
        by_walmart = seller is None or seller.lower() in {"walmart.com", "walmart"}
        offer = _offer(
            name[:200],
            "walmart.com",
            price,
            "https://www.walmart.com" + path,
            None,
            item.get("averageRating"),
            item.get("numberOfReviews"),
            "walmart_search_page",
        )
        offers.append(
            offer.copy(
                update={
                    "trust": "high" if by_walmart else "medium",
                    "seller": None if by_walmart else seller,
                    "condition": "used/refurb" if used else "new",
                    "note": None if by_walmart else f"Marketplace seller on Walmart: {seller}",
                }
            )
        )
    return offers


_WALMART: dict[tuple, tuple[float, list[Offer]]] = {}


async def walmart_page_search(query: str, max_price: float | None = None) -> list[Offer]:
    from .merchants import fetch_page

    if not _words(query):
        return []
    key = (" ".join(query.lower().split()), max_price)
    cached = _WALMART.get(key)
    if cached and time.monotonic() - cached[0] < CACHE_SECONDS:
        return cached[1]
    page = await fetch_page(
        walmart_search_url(query), limit=4_000_000, timeout=aiohttp.ClientTimeout(total=3.5)
    )
    offers = parse_walmart_results(page, max_price) if page else []
    if offers:
        while len(_WALMART) >= CACHE_SIZE:
            del _WALMART[next(iter(_WALMART))]
        _WALMART[key] = (time.monotonic(), offers)
    return offers
