"""Structured offer reads from explicitly reviewed HTTPS merchant domains only.

No arbitrary URL fetches, no redirect following, and no execution/navigation.
Returned search prices must match a product's JSON-LD Offer in USD.
"""

import json
import re
from urllib.parse import urlsplit

import aiohttp
from lxml import html

from .models import Offer

DOMAINS = {
    "www.amazon.com",
    "www.bestbuy.com",
    "www.walmart.com",
    "www.target.com",
    "www.soundcore.com",
    "www.sony.com",
    "electronics.sony.com",
    "www.bose.com",
}
LIMIT = 2_000_000


def allowed(url: str) -> bool:
    try:
        parsed = urlsplit(url)
        return (
            parsed.scheme == "https"
            and parsed.hostname in DOMAINS
            and parsed.port in {None, 443}
            and not parsed.username
            and not parsed.password
        )
    except ValueError:
        return False


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


def parse_offers(page: str, url: str, product: str) -> list[Offer]:
    from .live import matches_product

    result = []
    for text in html.fromstring(page).xpath(
        '//script[@type="application/ld+json"]/text()'
    ):
        try:
            nodes = list(products(json.loads(text)))
        except (ValueError, TypeError):
            continue
        for node in nodes:
            name = str(node.get("name", ""))
            if not matches_product(name, product) or re.search(
                r"replacement|ear\s*pads?|cushions?|carrying case|cover|charger|cable|adapter|refurbished|renewed",
                name,
                re.IGNORECASE,
            ):
                continue
            offers = node.get("offers", [])
            if isinstance(offers, dict):
                offers = [offers]
            for offer in offers:
                # Aggregate ranges and promotional subscriptions cannot establish a SKU offer.
                if not isinstance(offer, dict) or offer.get("@type") != "Offer":
                    continue
                if offer.get("priceCurrency") != "USD":
                    continue
                condition = str(offer.get("itemCondition", ""))
                if condition and not condition.endswith("NewCondition"):
                    continue
                availability = str(offer.get("availability", ""))
                if availability and not availability.endswith("InStock"):
                    continue
                try:
                    price = float(offer["price"])
                except (KeyError, ValueError, TypeError):
                    continue
                if not 0 < price < 1_000_000:
                    continue
                result.append(
                    Offer(
                        product=product,
                        store=urlsplit(url).hostname,
                        price=price,
                        url=url,
                    )
                )
    return result


async def fetch_offers(url: str, product: str) -> list[Offer]:
    if not allowed(url):
        return []
    try:
        async with (
            aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=12)) as session,
            session.get(
                url, allow_redirects=False, headers={"User-Agent": "FocalResearch/0.1"}
            ) as response,
        ):
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
            return parse_offers(
                b"".join(chunks).decode("utf-8", errors="replace"), url, product
            )
    except (TimeoutError, aiohttp.ClientError, ValueError):
        return []
