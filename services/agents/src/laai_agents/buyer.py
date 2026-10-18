"""Amazon cart-only buying agent. Real cart additions; checkout and payment disabled."""

import asyncio
import os
import re
import secrets
import time
from collections import OrderedDict
from typing import Literal
from urllib.parse import quote, urlsplit
from uuid import uuid4

from pydantic.v1 import Field, SecretStr, root_validator
from uagents import Model

from . import catalog, llm
from .agents import make_agent
from .chat import build_chat_protocol
from .live import names_product
from .models import EvidenceResponse
from .progress import report, tracked
from .search import search

_quotes = OrderedDict()


class BuyRequest(Model):
    product: str | None = Field(default=None, min_length=1, max_length=500)
    url: str | None = Field(default=None, max_length=2048)
    quantity: int = Field(default=1, ge=1, le=20)
    progress_id: str | None = Field(default=None, min_length=32, max_length=64)

    @root_validator(skip_on_failure=True)
    def input_required(cls, values):
        if not values.get("product") and not values.get("url"):
            raise ValueError("Supply a product name or Amazon product URL")
        return values


class BuyResponse(EvidenceResponse):
    product: str
    quantity: int = 1
    retailer: str = "amazon"
    demo: bool = True
    real_cart: bool = True
    purchase_enabled: Literal[False] = False
    notice: str | None = (
        "Real cart additions only after approval. Checkout, payment and order submission are disabled."
    )
    status: str
    selected_url: str | None = None
    search_url: str | None = None
    candidates: list[dict] = Field(default_factory=list)
    confirmation_token: str | None = None
    cart_url: str | None = None
    summary: str


class BuyConfirmation(Model):
    confirmation_token: str = Field(min_length=32, max_length=64)
    executor_token: SecretStr = SecretStr("")
    progress_id: str | None = Field(default=None, min_length=32, max_length=64)


def amazon_listing(url):
    try:
        parsed = urlsplit(url)
        if (
            parsed.scheme != "https"
            or parsed.hostname not in {"amazon.com", "www.amazon.com"}
            or parsed.username
            or parsed.password
            or parsed.port not in {None, 443}
        ):
            return None
        # Search engines now mostly return Amazon's /clp/<ASIN> landing pages.
        match = re.search(
            r"/(?:dp|gp/product|gp/aw/d|clp)/([A-Z0-9]{10})(?:/|$)", parsed.path, re.IGNORECASE
        )
        return "https://www.amazon.com/dp/" + match[1].upper() if match else None
    except ValueError:
        return None


LISTING_SECONDS = 4
MAX_CANDIDATES = 8


async def amazon_listings(name: str) -> list[dict]:
    """Listings on Amazon's search page that are this product: new ones first, used or
    renewed ones labeled. Each carries its ASIN link and the listed price."""
    from .merchants import is_accessory
    from .stores import amazon_page_search

    offers = [
        o
        for o in await amazon_page_search(name)
        if names_product(o.product, name) and not is_accessory(o.product, name)
    ]
    offers.sort(key=lambda o: (o.condition != "new", o.price))
    return [
        {
            "title": o.product,
            "url": o.url,
            "price": o.price,
            "condition": o.condition,
            "rating": o.rating,
            "source": "amazon_search_page",
        }
        for o in offers
        if amazon_listing(o.url)
    ]


def prune():
    for token in list(_quotes):
        if _quotes[token]["expires"] < time.monotonic():
            del _quotes[token]


@tracked("buyer", subject="Amazon listing")
async def prepare(req: BuyRequest) -> BuyResponse:
    prune()
    name = req.product or "Amazon product"
    known = catalog.find_product(name)
    if known:
        name = known.name
    report("validating", "Checking retailer and product input; cart only, no checkout")
    if req.url:
        url = amazon_listing(req.url)
        if not url:
            return BuyResponse(
                product=name,
                status="needs_listing",
                data_source="user_input",
                error="Use an HTTPS amazon.com product link containing an ASIN.",
                summary="Please provide a full Amazon product link or a product name.",
            )
        token = uuid4().hex
        if len(_quotes) >= 128:
            _quotes.popitem(last=False)
        result = BuyResponse(
            product=name,
            quantity=req.quantity,
            selected_url=url,
            status="cart_review",
            confirmation_token=token,
            data_source="amazon_cart_plan",
            notice="Adds to a real Amazon cart only after approval. Verify listing and quantity; checkout and payment are disabled.",
            summary=f"Ready to add this Amazon listing to your real cart; requested quantity {req.quantity}. "
            "Review the listing, then confirm add to cart. The agent stops at the cart and never places an order.",
        )
        _quotes[token] = {"expires": time.monotonic() + 600, "result": result}
        report("review_ready", "Cart addition ready; awaiting your approval")
        return result
    if not known:
        name = await llm.extract_product(name)
    # Amazon's own search page (real listings with ASIN and price) and a web search for
    # amazon.com links run together; neither may hold the answer up for long.
    listed, found = await asyncio.gather(
        asyncio.wait_for(amazon_listings(name), LISTING_SECONDS),
        asyncio.wait_for(search(f"site:amazon.com {name}"), LISTING_SECONDS),
        return_exceptions=True,
    )
    if isinstance(listed, BaseException) and not isinstance(listed, Exception):
        raise listed
    if isinstance(found, BaseException) and not isinstance(found, Exception):
        raise found
    listed = [] if isinstance(listed, BaseException) else listed
    if isinstance(found, BaseException):
        if not listed:
            return BuyResponse(
                product=name,
                status="needs_listing",
                data_source="unavailable",
                error="Amazon listing search unavailable",
                summary="Search is unavailable. Paste the Amazon product link to prepare a cart addition.",
            )
        sources, meta = [], {"data_source": "amazon_search_page", "sources": [], "notice": None}
    else:
        sources, meta = found
    candidates = {c["url"]: c for c in listed}
    for source in sources:
        url = amazon_listing(source.url)
        title = re.sub(r"^Amazon\.com\s*:\s*", "", source.title)
        if url and url not in candidates and names_product(title, name):
            candidates[url] = {"title": title, "url": url}
    return BuyResponse(
        product=name,
        quantity=req.quantity,
        status="choose_listing",
        candidates=list(candidates.values())[:MAX_CANDIDATES],
        search_url="https://www.amazon.com/s?k=" + quote(name),
        summary=(
            "Select an Amazon listing to prepare a real cart addition. "
            if candidates
            else "No direct Amazon listings were found. Search Amazon and paste a product link. "
        )
        + "Nothing is added until you approve; checkout and payment remain disabled.",
        **meta,
    )


@tracked("buyer", subject="Amazon listing")
async def confirm(req: BuyConfirmation) -> BuyResponse:
    expected = os.getenv("AMAZON_CART_EXECUTOR_TOKEN", "")
    if not expected or not secrets.compare_digest(
        expected, req.executor_token.get_secret_value()
    ):
        return BuyResponse(
            product="Amazon product",
            status="not_connected",
            data_source="amazon_cart",
            error="Cart executor is not configured or authorized",
            summary="The local Amazon cart browser executor is not connected. No cart was changed.",
        )
    prune()
    quote_data = _quotes.get(req.confirmation_token)
    if not quote_data:
        return BuyResponse(
            product="Amazon product",
            status="expired",
            data_source="amazon_cart",
            error="Cart approval expired; prepare it again",
            summary="Prepare a fresh cart addition before confirming.",
        )
    result = quote_data["result"]
    if result.status == "cart_added":
        return result
    if quote_data.get("started"):
        return result.copy(
            update={
                "status": "cart_unverified",
                "error": "Cart addition already attempted",
                "summary": "This cart approval was already used. Inspect Amazon before preparing another addition.",
            }
        )
    quote_data["started"] = True
    from .amazon_cart import add_to_cart

    outcome = await add_to_cart(result.selected_url, result.quantity)
    result = result.copy(
        update={
            "status": outcome["status"],
            "summary": outcome["message"],
            "cart_url": outcome.get("cart_url"),
            "data_source": "amazon_browser",
            "error": None if outcome["status"] == "cart_added" else outcome["message"],
        }
    )
    quote_data["result"] = result
    return result


class CartAddRequest(Model):
    """A spoken "add <product> to my Amazon cart" from the glasses: the command is the approval."""

    product: str = Field(min_length=2, max_length=200)
    executor_token: SecretStr = SecretStr("")


class CartAddResponse(Model):
    product: str
    status: str
    summary: str
    purchase_enabled: bool = False


class CartShowRequest(Model):
    """Opens the cart window on the cart page (nothing is added)."""

    executor_token: SecretStr = SecretStr("")


class CartListingRequest(Model):
    """The page watcher's approved listing, added here because this process owns the cart browser."""

    listing: dict
    executor_token: SecretStr = SecretStr("")


def _executor_ok(token: SecretStr) -> bool:
    expected = os.getenv("AMAZON_CART_EXECUTOR_TOKEN", "")
    return bool(expected) and secrets.compare_digest(expected, token.get_secret_value())


async def add_approved_listing(req: CartListingRequest) -> CartAddResponse:
    from . import cart_action

    listing = req.listing
    asin = listing.get("asin")
    title = str(listing.get("title") or "Amazon product")[:200]
    if not _executor_ok(req.executor_token):
        return CartAddResponse(product=title, status="not_connected",
                               summary="The laptop's Amazon cart isn't connected. Nothing was added.")
    price = listing.get("price")
    if not (isinstance(asin, str) and re.fullmatch(r"[A-Z0-9]{10}", asin)
            and listing.get("url") == f"https://www.amazon.com/dp/{asin}"
            and isinstance(price, (int, float)) and isinstance(listing.get("condition"), str)):
        return CartAddResponse(product=title, status="refused", summary="Not a valid Amazon listing. Nothing was added.")
    try:
        summary = await cart_action.add_listing({**listing, "title": title})
    except cart_action.CartError as exc:
        return CartAddResponse(product=title, status="failed", summary=str(exc))
    return CartAddResponse(product=title, status="cart_added", summary=summary)


VOICE_PICK_SECONDS = 25


async def add_product(req: CartAddRequest) -> CartAddResponse:
    """Picks the one Amazon listing for the product (cart_action.pick_listing, as the page
    watcher does) and adds it on the laptop. Never starts checkout."""
    from . import cart_action

    if not _executor_ok(req.executor_token):
        return CartAddResponse(
            product=req.product, status="not_connected",
            summary="The laptop's Amazon cart isn't connected. Nothing was added.",
        )
    # Asked for by voice, so it can take longer than the watcher's quick proposals.
    listing = await cart_action.pick_listing({}, req.product, VOICE_PICK_SECONDS)
    if not listing:
        return CartAddResponse(
            product=req.product, status="not_found",
            summary=f"Couldn't find {req.product} with a price on Amazon. Nothing was added.",
        )
    try:
        summary = await cart_action.add_listing(listing)
    except cart_action.CartError as exc:
        return CartAddResponse(product=listing["title"], status="failed", summary=str(exc))
    return CartAddResponse(product=listing["title"], status="cart_added", summary=summary)


class CartApproval(Model):
    """Agent-to-agent cart approval; accepted only from the coordinator."""

    confirmation_token: str = Field(min_length=32, max_length=64)


def build_buyer_agent(port=None):
    from uagents import Protocol

    from .agents import coordinator_address

    agent = make_agent(
        "focal-amazon-buyer-demo",
        os.getenv("BUYER_AGENT_SEED", "laai buyer demo seed"),
        port,
    )
    cart = Protocol(name="AmazonCart", version="0.1.0")

    @cart.on_message(BuyRequest, replies=BuyResponse)
    async def on_buy_request(ctx, sender, req):
        await ctx.send(sender, await prepare(req))

    # The executor token stays in this process; the coordinator only relays the
    # shopper's approval from ASI:One.
    @cart.on_message(CartApproval, replies=BuyResponse)
    async def on_cart_approval(ctx, sender, req):
        if sender != coordinator_address():
            await ctx.send(
                sender,
                BuyResponse(
                    product="Amazon product",
                    status="refused",
                    error="Only the coordinator can approve cart additions",
                    summary="Refused: approvals come only from the coordinator.",
                ),
            )
            return
        await ctx.send(
            sender,
            await confirm(
                BuyConfirmation(
                    confirmation_token=req.confirmation_token,
                    executor_token=os.getenv("AMAZON_CART_EXECUTOR_TOKEN", ""),
                )
            ),
        )

    agent.include(cart, publish_manifest=True)

    @agent.on_rest_post("/buy/prepare", BuyRequest, BuyResponse)
    async def on_prepare(ctx, req):
        return await prepare(req)

    @agent.on_rest_post("/buy/confirm", BuyConfirmation, BuyResponse)
    async def on_confirm(ctx, req):
        return await confirm(req)

    @agent.on_rest_post("/cart/add", CartAddRequest, CartAddResponse)
    async def on_cart_add(ctx, req):
        return await add_product(req)

    @agent.on_rest_post("/cart/clear", CartShowRequest, CartAddResponse)
    async def on_cart_clear(ctx, req):
        if not _executor_ok(req.executor_token):
            return CartAddResponse(product="Amazon cart", status="not_connected", summary="Not authorized.")
        from .amazon_cart import clear_cart

        cleared = await clear_cart()
        return CartAddResponse(product="Amazon cart", status=cleared["status"], summary=cleared["message"])

    @agent.on_rest_post("/cart/show", CartShowRequest, CartAddResponse)
    async def on_cart_show(ctx, req):
        if not _executor_ok(req.executor_token):
            return CartAddResponse(product="Amazon cart", status="not_connected", summary="Not authorized.")
        from .amazon_cart import show_cart

        shown = await show_cart()
        return CartAddResponse(product="Amazon cart", status=shown["status"], summary=shown["message"])

    @agent.on_rest_post("/cart/add_listing", CartListingRequest, CartAddResponse)
    async def on_cart_add_listing(ctx, req):
        return await add_approved_listing(req)

    async def answer(ctx, text):
        link = re.search(r"https://[^\s]+", text)
        result = await prepare(
            BuyRequest(product=text[:500], url=link[0] if link else None)
        )
        return result.summary + "\n" + "\n".join(c["url"] for c in result.candidates)

    agent.include(
        build_chat_protocol(answer, extract_product=False), publish_manifest=True
    )
    return agent
