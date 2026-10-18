"""`add_to_amazon_cart`: the page watcher's approval-gated Amazon cart action.

`pick_listing` names one exact Amazon listing before anything is proposed: on an
amazon.com /dp/<ASIN> page that ASIN; otherwise buyer.prepare's Amazon listings for the
product, new before used/refurb, cheapest first, that name the same model (every model
number in the product name). The listing's own page is read, when Amazon serves it, for
the buy-box price, seller and title. No listing with a price means no proposal.

`add_listing` runs only for an approved action, with the listing journaled at proposal
time (never anything from the event or the page). It adds exactly that ASIN through
amazon_cart.add_to_cart, which verifies it in the active cart and never starts checkout.
AMAZON_CART_DRY_RUN is on unless set to "0": a dry run reports what it would add and never
opens a browser.
"""

import asyncio
import os
import re
import time

import aiohttp

from . import buyer
from .merchants import condition_of

PICK_SECONDS = 8
PAGE_SECONDS = 4
# Listing pages read to verify a pick (in parallel).
VERIFY = 4
CACHE_SECONDS = 600
_picks: dict[tuple, tuple[float, dict | None]] = {}

PRICE_BOXES = ("corePriceDisplay_desktop_feature_div", "corePrice_feature_div", "corePrice_desktop")
# Elements naming the seller; the first one with text counts (Amazon repeats the merchant
# line for its desktop and mobile layouts, so joining every match doubled the name).
SELLER_PATHS = (
    '//*[@id="sellerProfileTriggerId"]',
    '//*[@id="merchantInfoFeature_feature_div"]//*[contains(@class,"offer-display-feature-text-message")]',
    '//*[@id="tabular-buybox"]//*[contains(@class,"tabular-buybox-text")][last()]',
)


def dry_run() -> bool:
    return os.getenv("AMAZON_CART_DRY_RUN", "1").strip() != "0"


def asin_of(url: str | None) -> str | None:
    listing = buyer.amazon_listing(url or "")
    return listing.rsplit("/", 1)[1] if listing else None


def same_model(title: str, product: str) -> bool:
    """The listing is this product: its words (buyer's names_product), and every model
    number exactly ("WH-1000XM4" is not "WH-1000XM5"), however Amazon spaces it."""
    from .live import names_product

    if not names_product(title, product):
        return False
    compact = re.sub(r"[^a-z0-9]", "", title.lower())
    models = [re.sub(r"[^a-z0-9]", "", w) for w in product.lower().split() if re.search(r"\d", w)]
    return all(m in compact for m in models)


def _money(text: str | None) -> float | None:
    match = re.fullmatch(r"\$\s*([\d,]+(?:\.\d{1,2})?)", (text or "").strip())
    return float(match[1].replace(",", "")) if match else None


def parse_listing_page(page: str, asin: str) -> dict | None:
    """Title, buy-box price, seller and condition from an amazon.com product page; None
    when the page is not that ASIN or shows no title."""
    from lxml import html

    from .merchants import MALFORMED

    try:
        doc = html.fromstring(page)
    except MALFORMED:
        return None
    shown = {v.strip().upper() for v in doc.xpath('//input[@id="ASIN"]/@value') if v.strip()}
    if shown and shown != {asin}:
        return None
    title = " ".join(" ".join(doc.xpath('//span[@id="productTitle"]//text()')).split())
    if not title:
        return None
    # What Add to Cart would add: the main button is the new offer; with only Amazon's
    # used buy box ("Buy Used", #add-to-cart-button-ubb) the add is a used item.
    new_offer = bool(doc.xpath('//*[@id="add-to-cart-button"]'))
    used_offer = bool(doc.xpath('//*[@id="add-to-cart-button-ubb"]'))
    if not new_offer and not used_offer:
        return None  # No Add to Cart on the page: unavailable, or not readable.
    price = None
    for box in PRICE_BOXES:
        paying = doc.xpath(f'//*[@id="{box}"]//*[contains(@class,"priceToPay")]')
        amounts = [re.sub(r"\s+", "", " ".join(p.xpath(".//text()"))) for p in paying] or [
            t
            for t in doc.xpath(
                f'//*[@id="{box}"]//span[contains(@class,"a-price") and '
                'not(contains(@class,"a-text-price"))]/span[@class="a-offscreen"]/text()'
            )
            if t.strip()
        ]
        if amounts:
            price = _money(amounts[0])
            break
    seller = None
    for path in SELLER_PATHS:
        text = next(
            (t for e in doc.xpath(path) if (t := " ".join(e.text_content().split()))), ""
        )
        if text:
            seller = text[:80]
            break
    condition = condition_of(title)
    if not new_offer:
        box = " ".join(
            " ".join(
                doc.xpath(
                    '//*[@id="buybox"]//text()[not(ancestor::script) and not(ancestor::style)]'
                )
            ).split()
        )
        grade = re.search(r"Used:\s*(Like New|Very Good|Good|Acceptable)", box)
        condition = f"used/refurb (Used: {grade[1]})" if grade else "used/refurb"
    return {
        "title": title[:200],
        "price": price,
        "seller": seller,
        "condition": condition,
    }


async def read_listing(url: str, asin: str) -> dict | None:
    from .merchants import fetch_page

    page = await fetch_page(
        url, limit=4_000_000, timeout=aiohttp.ClientTimeout(total=PAGE_SECONDS)
    )
    return parse_listing_page(page, asin) if page else None


def _page_price(page: dict) -> float | None:
    for fact in page.get("facts") or []:
        if str(fact.get("label", "")).lower() == "price":
            match = re.search(r"\$\s*[\d,]+(?:\.\d{1,2})?", str(fact.get("value", "")))
            if match:
                return _money(match[0])
    return None


async def _search_candidates(product: str) -> list[dict]:
    """buyer.prepare's Amazon listings that are this model: priced ones (from Amazon's own
    search page) first, new before used/refurb, cheapest first; then unpriced web-search ones."""
    result = await buyer.prepare(buyer.BuyRequest(product=product))
    name = result.product or product
    found = [
        c
        for c in result.candidates
        if asin_of(c.get("url")) and same_model(c.get("title", ""), name)
    ]
    found.sort(
        key=lambda c: (
            c.get("price") is None,
            c.get("condition", "new") != "new",
            c.get("price") or 0,
        )
    )
    return found


def _listing(candidate: dict, read: dict | None, on_amazon_page: bool) -> dict:
    asin = asin_of(candidate["url"])
    read = read or {}
    return {
        "asin": asin,
        "url": f"https://www.amazon.com/dp/{asin}",
        "title": (read.get("title") or candidate["title"])[:200],
        "price": read.get("price") if read.get("price") is not None else candidate.get("price"),
        "seller": read.get("seller"),
        "condition": read.get("condition") or candidate.get("condition", "new"),
        "on_amazon_page": on_amazon_page,
    }


async def _pick(page: dict, product: str) -> dict | None:
    asin = asin_of(page.get("url"))
    if asin:
        # The user is on this listing: add exactly it.
        url = f"https://www.amazon.com/dp/{asin}"
        read = await read_listing(url, asin)
        listing = {
            "asin": asin,
            "url": url,
            "title": (read or {}).get("title") or page.get("title", product)[:200],
            "price": (read or {}).get("price") or _page_price(page),
            "seller": (read or {}).get("seller"),
            "condition": (read or {}).get("condition") or condition_of(page.get("title", "")),
            "on_amazon_page": True,
        }
        if listing["price"] is None:
            # Amazon's search page may still list this ASIN with its price.
            same = [
                c
                for c in await _search_candidates(product)
                if asin_of(c["url"]) == asin and c.get("price") is not None
            ]
            if not same:
                return None
            listing["price"] = same[0]["price"]
        return listing
    candidates = await _search_candidates(product)
    # Verify the leading listings on their own pages (buy-box price, seller, title); the
    # search card can lag and web-search hits carry no price. Pages read in parallel.
    leading = candidates[:VERIFY]
    reads = await asyncio.gather(
        *(read_listing(f"https://www.amazon.com/dp/{asin_of(c['url'])}", asin_of(c["url"]))
          for c in leading)
    )
    verified = [
        _listing(c, r, False)
        for c, r in zip(leading, reads, strict=True)
        if r and r.get("price") is not None and same_model(r["title"], product)
    ]
    if verified:
        return min(verified, key=lambda v: (v["condition"] != "new", v["price"]))
    # Unverified: only a listing priced on Amazon's own search page, seller unknown.
    priced = [c for c in candidates if c.get("price") is not None]
    return _listing(priced[0], None, False) if priced else None


async def pick_listing(page: dict, product: str) -> dict | None:
    """The one Amazon listing the cart action would add, or None (then no action)."""
    key = (product.lower(), asin_of(page.get("url")))
    cached = _picks.get(key)
    if cached and time.monotonic() - cached[0] < CACHE_SECONDS:
        return cached[1]
    try:
        listing = await asyncio.wait_for(_pick(page, product), PICK_SECONDS)
    except Exception:  # noqa: BLE001 -- no listing means no cart action, never a failed proposal
        return None
    while len(_picks) >= 64:
        del _picks[next(iter(_picks))]
    _picks[key] = (time.monotonic(), listing)
    return listing


def describe(listing: dict) -> str:
    seller = (
        f"sold by {listing['seller']}"
        if listing.get("seller")
        else "seller not shown (check on Amazon)"
    )
    return (
        f"Add to your Amazon cart: {listing['title']} (ASIN {listing['asin']}), "
        f"${listing['price']:,.2f}, {listing['condition']}, {seller}. "
        "Quantity 1. Checkout is not started."
    )[:2000]


async def add_listing(listing: dict) -> str:
    """Add the approved listing; returns the result message. Raises on failure so the
    watcher posts a failed result with this exact text."""
    title, price = listing["title"], f"${listing['price']:,.2f}"
    if dry_run():
        return (
            f"Dry run: would add {title} ({price}, ASIN {listing['asin']}) to your "
            "Amazon cart. Nothing was added. Checkout not started."
        )
    from .amazon_cart import add_to_cart

    # A listing approved as new must not become Amazon's used offer at add time.
    outcome = await add_to_cart(listing["url"], 1, require_new=listing["condition"] == "new")
    if outcome["status"] == "cart_added":
        if outcome["message"].startswith("The selected item is already"):
            return (
                f"{title} is already in your Amazon cart; no duplicate added. "
                "Checkout not started."
            )
        return f"Added {title} ({price}) to your Amazon cart. Checkout not started."
    if outcome["status"] == "needs_sign_in":
        raise CartError(
            "Needs Amazon sign-in: sign in once in the Chrome window that opened on the "
            "laptop, then open this product again to add it. Nothing was added."
        )
    if outcome["status"] == "cart_unverified":
        raise CartError(f"Could not confirm {title} in your Amazon cart. {outcome['message']}")
    raise CartError(f"Not added to your Amazon cart. {outcome['message']}")


class CartError(RuntimeError):
    """A cart outcome the user should read as-is (sign-in, verification, blocked)."""
