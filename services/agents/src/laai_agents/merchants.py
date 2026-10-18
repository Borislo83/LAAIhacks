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
import time
from statistics import median
from urllib.parse import urljoin, urlsplit

import aiohttp
from lxml import etree, html

from . import net
from .models import Offer, Source

# lxml's parser errors ("Document is empty") are not ValueErrors.
MALFORMED = (ValueError, etree.LxmlError)

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
TIMEOUT = aiohttp.ClientTimeout(total=7)
# Price lookups wait at most this long for one store page; slow stores are skipped.
PAGE_TIMEOUT = aiohttp.ClientTimeout(total=2.5)
HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/130.0 Safari/537.36",
    "Accept": "text/html,application/xhtml+xml",
    "Accept-Language": "en-US,en;q=0.9",
}
WRONG_ITEM = re.compile(
    r"replacement|ear\s*pads?|ear\s*tips?|cushions?|carrying case|cover|skin|"
    r"charger|cable|adapter",
    re.IGNORECASE,
)
# Not new: shown with a "used/refurb" label, never picked as the best price.
USED = re.compile(
    r"\b(?:renewed|refurbished|refurb|restored|pre-?owned|open[- ]box|"
    r"(?:certified\s+)?used\s*(?:-|–|:|\)|,|\])|like\s+new)|\(used\b|^\s*used\b",
    re.IGNORECASE,
)


BUNDLE = re.compile(r"\bbundle\b|\s\+\s", re.IGNORECASE)


def condition_of(title: str) -> str:
    return "used/refurb" if USED.search(title or "") else "new"

ACCESSORY = re.compile(
    r"\b(?:screen\s+protectors?|protectors?|keychains?|stickers?|decals?|straps?|lanyards?|"
    r"skins?|sleeves?|pouch(?:es)?)\b",
    re.IGNORECASE,
)


def is_accessory(title: str, product: str) -> bool:
    """A listing made *for* the product ("ESR Case for AirPods Pro 2", "Band compatible
    with Forerunner 55"), not the product. "Earbuds with charging case" is the product."""
    if WRONG_ITEM.search(title) or ACCESSORY.search(title):
        return True
    # "Case for AirPods", "Band compatible with Galaxy Watch", "for AirPods Pro 2 Case".
    if not re.search(r"charging\s+case", title, re.IGNORECASE) and re.search(
        r"\b(?:cases?|bands?|covers?|stands?|holders?|docks?|mounts?|tips|hooks?)\b.{0,40}"
        r"\b(?:for|compatible\s+with)\b|\b(?:for|compatible\s+with)\b.{0,40}\b(?:cases?|bands?)\b",
        title,
        re.IGNORECASE,
    ):
        return True
    from .live import GENERIC_WORDS, matches_product

    words = [w for w in re.findall(r"[a-z0-9]+", product.lower()) if w not in GENERIC_WORDS]
    for word in words[:3]:
        made_for = re.search(
            rf"\b(?:for|compatible\s+with|fits|designed\s+for|made\s+for)\s+"
            rf"(?:[\w-]+\s+){{0,2}}{re.escape(word)}\b",
            title,
            re.IGNORECASE,
        )
        # "AirPods Pro 2 ... for iPhone 15 Pro" names the product first: it is the product.
        if made_for and not matches_product(title[: made_for.start()], product):
            return True
    return False


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

    def add(price: float | None, source: str, condition: str = "new") -> None:
        if price is not None:
            result.append(
                Offer(
                    product=product,
                    store=store,
                    price=price,
                    url=url,
                    source=source,
                    trust=trust_for(url, source),
                    condition=condition,
                )
            )

    for text in doc.xpath('//script[@type="application/ld+json"]/text()'):
        try:
            nodes = list(products(json.loads(text)))
        except (ValueError, TypeError):
            continue
        for node in nodes:
            name = str(node.get("name", ""))
            if not matches_product(name, product) or is_accessory(name, product):
                continue
            offers = node.get("offers", [])
            for offer in [offers] if isinstance(offers, dict) else offers:
                if isinstance(offer, dict):
                    state = str(offer.get("itemCondition", ""))
                    used = (state and not state.endswith("NewCondition")) or USED.search(name)
                    add(_offer_price(offer), "structured", "used/refurb" if used else "new")
    if result:
        return result
    store_page = _walmart_product(doc, url, product) or _amazon_product(doc, url, product)
    if store_page:
        return store_page

    # Many stores omit JSON-LD but still publish Open Graph / microdata price tags.
    title = " ".join(
        doc.xpath('//meta[@property="og:title"]/@content') + doc.xpath("//title/text()")
    )
    if not matches_product(title, product) or is_accessory(title, product):
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
        add(_price(amounts[0]), "page_meta", condition_of(title))
    return result


def _walmart_product(doc, url: str, product: str) -> list[Offer]:
    """A walmart.com /ip/ page: its embedded product JSON has the buy-box price and seller."""
    from .live import matches_product

    if store_of(url) != "walmart.com" or "/ip/" not in url:
        return []
    try:
        data = json.loads(doc.xpath('//script[@id="__NEXT_DATA__"]/text()')[0])
        item = data["props"]["pageProps"]["initialData"]["data"]["product"]
        current = item["priceInfo"]["currentPrice"]
    except (IndexError, KeyError, TypeError, ValueError):
        return []
    name = str(item.get("name", ""))
    price = _price(current.get("price"))
    if price is None or current.get("currencyUnit", "USD") != "USD":
        return []
    if not matches_product(name, product) or is_accessory(name, product):
        return []
    if str(item.get("availabilityStatus", "IN_STOCK")) != "IN_STOCK":
        return []
    seller = " ".join(str(item.get("sellerDisplayName") or item.get("sellerName") or "").split())[:80]
    by_walmart = not seller or seller.lower() in {"walmart.com", "walmart"}
    used = item.get("isConditionNew") is False or condition_of(name) != "new"
    return [
        Offer(
            product=product, store="walmart.com", price=price, url=url.split("?")[0],
            source="structured", trust="high" if by_walmart else "medium",
            seller=None if by_walmart else seller,
            note=None if by_walmart else f"Marketplace seller on Walmart: {seller}",
            condition="used/refurb" if used else "new",
        )
    ]  # fmt: skip


def _amazon_product(doc, url: str, product: str) -> list[Offer]:
    """An amazon.com product page: the buy-box price next to the product title. A page
    with no buy-box price (unavailable new) gives nothing; other products' prices on the
    page (ads, "compare with similar items") are never read."""
    from .live import matches_product

    if store_of(url) != "amazon.com":
        return []
    title = " ".join(" ".join(doc.xpath('//span[@id="productTitle"]//text()')).split())
    if not title or not matches_product(title, product) or is_accessory(title, product):
        return []
    for box in ("corePriceDisplay_desktop_feature_div", "corePrice_feature_div", "corePrice_desktop"):
        amounts = [
            t.strip()
            for t in doc.xpath(
                f'//*[@id="{box}"]//span[contains(@class,"a-price") and '
                'not(contains(@class,"a-text-price"))]/span[@class="a-offscreen"]/text()'
            )
            if t.strip()
        ]
        if amounts:
            price = _price(amounts[0].replace("$", "")) if amounts[0].startswith("$") else None
            if price is None:
                return []
            return [
                Offer(product=product, store="amazon.com", price=price, url=url,
                      source="structured", condition=condition_of(title))
            ]  # fmt: skip
    return []


# Brand, search and category pages list many products; a price there may be any of them.
LISTING_PATH = re.compile(
    r"/(?:brand|brands|search|browse|category|categories|cp|b|s|c|sch|deals?)(?:/|$)",
    re.IGNORECASE,
)


def listing_page(url: str) -> bool:
    parts = urlsplit(url)
    return bool(
        LISTING_PATH.search(parts.path)
        or re.search(r"(?:^|&)(?:q|query|k|_nkw|searchTerm|st)=", parts.query)
    )


def snippet_offer(source: Source, product: str) -> Offer | None:
    """A search snippet quoting exactly one plain dollar price becomes a lead."""
    text = source.snippet
    if not fetchable(source.url) or RANGE.search(text) or listing_page(source.url):
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
        condition=condition_of(f"{source.title} {text}"),
    )


def rank(offers: list[Offer]) -> list[Offer]:
    """Keep one offer per store, seller and price, drop clear price outliers, trust first.

    Prices read from product pages anchor the expected range when there are any;
    otherwise all candidates do (with at least three, so one stray can't skew it).
    Within a trust tier new offers come before used/refurbished ones.
    """
    unique = list({(o.store, o.seller, o.price, o.condition): o for o in offers}.values())
    # Search engines sometimes merge results, so a snippet can carry another site's
    # price. A snippet price identical to one read from a different site's page is a copy.
    read = {(o.price, o.store) for o in unique if o.source != "search_snippet"}
    unique = [
        o
        for o in unique
        if o.source != "search_snippet"
        or not any(price == o.price and store != o.store for price, store in read)
    ]
    new = [o for o in unique if o.condition == "new"] or unique
    page = [o.price for o in new if o.source != "search_snippet"]
    anchor = page or ([o.price for o in new] if len(new) >= 3 else [])
    if anchor:
        mid = median(anchor)
        unique = [o for o in unique if 0.4 * mid <= o.price <= 2.5 * mid]
    return sorted(
        (labeled(o) for o in unique),
        key=lambda o: (TRUST_ORDER[o.trust], o.condition != "new", o.price),
    )


def labeled(offer: Offer) -> Offer:
    """Mark whether the price is verified, and say why not when it isn't."""
    verified = offer.trust == "high" and offer.condition == "new"
    note = None if verified else offer.note
    if BUNDLE.search(offer.product):
        note = "Bundle: includes extras, so it isn't a like-for-like price"
    elif re.search(r"\bwithout\s+(?:a\s+|the\s+)?(?:manufacturer(?:'s)?\s+)?warranty", offer.product, re.IGNORECASE):
        note = "Sold without manufacturer warranty"
    if not note and not verified:
        if offer.condition != "new":
            note = "Used or refurbished listing"
        elif offer.source == "search_snippet":
            note = "Price from a search listing; not confirmed on the store page"
        elif not verified:
            note = "Price read from a store not on the major-retailer list"
    return offer.copy(update={"verified": verified, "note": note})


def best_offer(offers: list[Offer]) -> Offer | None:
    """Cheapest new offer from the most trusted tier that has one."""
    for tier in TRUST_ORDER:
        tiered = [o for o in offers if o.trust == tier and o.condition == "new"]
        if tiered:
            return min(tiered, key=lambda o: o.price)
    return None


# Stores that just refused or timed out are skipped for a while instead of re-waited on.
_BLOCKED: dict[str, float] = {}
BLOCKED_SECONDS = 600


def blocked(url: str) -> bool:
    at = _BLOCKED.get(store_of(url))
    return at is not None and time.monotonic() - at < BLOCKED_SECONDS


async def fetch_page(url: str, limit: int = LIMIT, timeout=TIMEOUT) -> str | None:
    """Read one public HTML page, re-checking every redirect; None if not readable."""
    try:
        for _ in range(MAX_REDIRECTS + 1):
            if not fetchable(url) or not await public_host(urlsplit(url).hostname):
                return None
            async with net.session().get(
                url, allow_redirects=False, headers=HEADERS, timeout=timeout
            ) as response:
                if response.status in {301, 302, 303, 307, 308}:
                    url = urljoin(url, response.headers.get("Location", ""))
                    continue
                if response.status != 200 or "text/html" not in response.headers.get(
                    "Content-Type", ""
                ):
                    return None
                chunks, size = [], 0
                async for chunk in response.content.iter_chunked(65536):
                    size += len(chunk)
                    if size > limit:
                        return None
                    chunks.append(chunk)
                page = b"".join(chunks).decode("utf-8", errors="replace")
                return page if page.strip() else None
        return None
    except (TimeoutError, aiohttp.ClientError, ValueError):
        return None


AMAZON_LANDING = re.compile(r"^https://(?:www\.)?amazon\.com/clp/([A-Z0-9]{10})(?:[/?]|$)")


async def fetch_offers(url: str, product: str) -> list[Offer]:
    # Search engines link Amazon's /clp/<ASIN> landing pages; the product page has the price.
    if landing := AMAZON_LANDING.match(url):
        url = f"https://www.amazon.com/dp/{landing[1]}"
    page = await fetch_page(url, limit=4_000_000, timeout=PAGE_TIMEOUT)
    if not page:
        _BLOCKED[store_of(url)] = time.monotonic()
    try:
        return parse_offers(page, url, product) if page else []
    except MALFORMED:
        return []
