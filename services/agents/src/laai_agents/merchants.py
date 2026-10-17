"""Two-stage price research: gather candidates broadly, then rank them by trust.

Stage 1 reads any public HTTPS search result: schema.org JSON-LD offers (Offer and
AggregateOffer), price meta tags, and a single clear dollar price quoted in a search
snippet. Stage 2 labels each candidate high/medium/low trust by store and evidence,
and drops only clear mismatches: other currencies, explicit out-of-stock or used
listings, installments, price ranges, and accessory-priced outliers.

Pages are fetched read-only (nothing is executed), redirects are re-checked, and
hosts that resolve to private or local network addresses are refused.
"""

import asyncio
import ipaddress
import json
import re
from statistics import median
from urllib.parse import urljoin, urlsplit

import aiohttp
from lxml import html

from .models import Offer, Source

# Major retailers and brand stores. Listing here raises trust; it never gates a fetch.
TRUSTED_STORES = {
    "amazon.com",
    "apple.com",
    "bestbuy.com",
    "walmart.com",
    "target.com",
    "costco.com",
    "newegg.com",
    "bhphotovideo.com",
    "adorama.com",
    "crutchfield.com",
    "samsung.com",
    "sony.com",
    "bose.com",
    "soundcore.com",
    "nothing.tech",
}
TRUST_ORDER = {"high": 0, "medium": 1, "low": 2}
LIMIT = 2_000_000
MAX_REDIRECTS = 3
HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/130.0 Safari/537.36",
    "Accept": "text/html,application/xhtml+xml",
    "Accept-Language": "en-US,en;q=0.9",
}
WRONG_ITEM = re.compile(
    r"replacement|ear\s*pads?|ear\s*tips?|cushions?|carrying case|cover|skin|"
    r"charger|cable|adapter|refurbished|renewed|pre-?owned",
    re.IGNORECASE,
)
DOLLARS = re.compile(r"(?<![\w$])\$\s?(\d{1,3}(?:,\d{3})+|\d+)(\.\d{2})?")
INSTALLMENT = re.compile(r"\s*(?:/|per\s+|a\s+)\s*mo(?:nth)?\b", re.IGNORECASE)
# "$35 orders", "save $50", "under $300": thresholds, discounts and budgets, not prices.
NOT_A_PRICE_AFTER = re.compile(
    r"\s*(?:\+|or more|orders?|off\b|savings?|gift|credit|back\b|shipping)",
    re.IGNORECASE,
)
NOT_A_PRICE_BEFORE = re.compile(
    r"(?:save|saving|over|under|up to|below|above|with|extra|get)\s*$", re.IGNORECASE
)
RANGE = re.compile(r"\$\s?[\d,.]+\s*(?:-|–|to)\s*\$", re.IGNORECASE)


def store_of(url: str) -> str:
    return (urlsplit(url).hostname or "").removeprefix("www.")


def trusted(url: str) -> bool:
    host = store_of(url)
    return any(host == d or host.endswith("." + d) for d in TRUSTED_STORES)


def trust_for(url: str, source: str) -> str:
    if source == "search_snippet":
        return "medium" if trusted(url) else "low"
    return "high" if trusted(url) else "medium"


def fetchable(url: str) -> bool:
    try:
        parsed = urlsplit(url)
        return (
            parsed.scheme == "https"
            and bool(parsed.hostname)
            and parsed.port in {None, 443}
            and not parsed.username
            and not parsed.password
        )
    except ValueError:
        return False


async def public_host(host: str) -> bool:
    try:
        infos = await asyncio.get_running_loop().getaddrinfo(host, 443)
        return bool(infos) and all(
            ipaddress.ip_address(info[4][0]).is_global for info in infos
        )
    except (OSError, ValueError):
        return False


def _price(value) -> float | None:
    try:
        price = float(str(value).replace(",", ""))
    except (TypeError, ValueError):
        return None
    return price if 0 < price < 1_000_000 else None


def products(node):
    if isinstance(node, list):
        for child in node:
            yield from products(child)
    elif isinstance(node, dict):
        types = node.get("@type", [])
        if types == "Product" or isinstance(types, list) and "Product" in types:
            yield node
        if "@graph" in node:
            yield from products(node["@graph"])
        if "mainEntity" in node:
            yield from products(node["mainEntity"])


def _offer_price(offer: dict) -> float | None:
    """A usable price from an Offer/AggregateOffer, or None if it clearly doesn't apply."""
    kind = offer.get("@type")
    if kind not in {"Offer", "AggregateOffer", None}:
        return None
    if offer.get("priceCurrency", "USD") != "USD":
        return None
    condition = str(offer.get("itemCondition", ""))
    if condition and not condition.endswith("NewCondition"):
        return None
    availability = str(offer.get("availability", ""))
    if re.search(r"OutOfStock|SoldOut|Discontinued", availability):
        return None
    if kind == "AggregateOffer":
        return _price(offer.get("lowPrice", offer.get("price")))
    return _price(offer.get("price"))


def parse_offers(page: str, url: str, product: str) -> list[Offer]:
    from .live import matches_product

    doc = html.fromstring(page)
    store = store_of(url)
    result = []

    def add(price: float | None, source: str) -> None:
        if price is not None:
            result.append(
                Offer(
                    product=product,
                    store=store,
                    price=price,
                    url=url,
                    source=source,
                    trust=trust_for(url, source),
                )
            )

    for text in doc.xpath('//script[@type="application/ld+json"]/text()'):
        try:
            nodes = list(products(json.loads(text)))
        except (ValueError, TypeError):
            continue
        for node in nodes:
            name = str(node.get("name", ""))
            if not matches_product(name, product) or WRONG_ITEM.search(name):
                continue
            offers = node.get("offers", [])
            for offer in [offers] if isinstance(offers, dict) else offers:
                if isinstance(offer, dict):
                    add(_offer_price(offer), "structured")
    if result:
        return result

    # Many stores omit JSON-LD but still publish Open Graph / microdata price tags.
    title = " ".join(
        doc.xpath('//meta[@property="og:title"]/@content') + doc.xpath("//title/text()")
    )
    if not matches_product(title, product) or WRONG_ITEM.search(title):
        return []
    currency = doc.xpath(
        '//meta[@property="product:price:currency" or @property="og:price:currency"]'
        "/@content"
    )
    if currency and currency[0].strip().upper() != "USD":
        return []
    amounts = doc.xpath(
        '//meta[@property="product:price:amount" or @property="og:price:amount"]'
        '/@content | //*[@itemprop="price"]/@content'
    )
    if amounts:
        add(_price(amounts[0]), "page_meta")
    return result


def snippet_offer(source: Source, product: str) -> Offer | None:
    """A search snippet quoting exactly one plain dollar price becomes a lead."""
    text = source.snippet
    if not fetchable(source.url) or RANGE.search(text):
        return None
    prices = {
        price
        for match in DOLLARS.finditer(text)
        if not INSTALLMENT.match(text, match.end())
        and not NOT_A_PRICE_AFTER.match(text, match.end())
        and not NOT_A_PRICE_BEFORE.search(text[max(0, match.start() - 12) : match.start()])
        and (price := _price(match.group(1) + (match.group(2) or ""))) is not None
    }
    if len(prices) != 1:
        return None
    return Offer(
        product=product,
        store=store_of(source.url),
        price=prices.pop(),
        url=source.url,
        source="search_snippet",
        trust=trust_for(source.url, "search_snippet"),
    )


def rank(offers: list[Offer]) -> list[Offer]:
    """Keep one offer per store and price, drop clear price outliers, trust first.

    Prices read from product pages anchor the expected range when there are any;
    otherwise all candidates do (with at least three, so one stray can't skew it).
    """
    unique = list({(o.store, o.price): o for o in offers}.values())
    page = [o.price for o in unique if o.source != "search_snippet"]
    anchor = page or ([o.price for o in unique] if len(unique) >= 3 else [])
    if anchor:
        mid = median(anchor)
        unique = [o for o in unique if 0.4 * mid <= o.price <= 2.5 * mid]
    return sorted(unique, key=lambda o: (TRUST_ORDER[o.trust], o.price))


def best_offer(offers: list[Offer]) -> Offer | None:
    """Cheapest offer from the most trusted tier that has one."""
    for tier in TRUST_ORDER:
        tiered = [o for o in offers if o.trust == tier]
        if tiered:
            return min(tiered, key=lambda o: o.price)
    return None


async def fetch_offers(url: str, product: str) -> list[Offer]:
    try:
        async with aiohttp.ClientSession(
            timeout=aiohttp.ClientTimeout(total=12), headers=HEADERS
        ) as session:
            for _ in range(MAX_REDIRECTS + 1):
                if not fetchable(url) or not await public_host(urlsplit(url).hostname):
                    return []
                async with session.get(url, allow_redirects=False) as response:
                    if response.status in {301, 302, 303, 307, 308}:
                        url = urljoin(url, response.headers.get("Location", ""))
                        continue
                    if response.status != 200 or "text/html" not in response.headers.get(
                        "Content-Type", ""
                    ):
                        return []
                    chunks, size = [], 0
                    async for chunk in response.content.iter_chunked(65536):
                        size += len(chunk)
                        if size > LIMIT:
                            return []
                        chunks.append(chunk)
                    page = b"".join(chunks).decode("utf-8", errors="replace")
                    return parse_offers(page, url, product) if page.strip() else []
            return []
    except (TimeoutError, aiohttp.ClientError, ValueError):
        return []
