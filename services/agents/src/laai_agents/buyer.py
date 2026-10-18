"""Amazon cart-only buying agent. Real cart additions; checkout and payment disabled."""

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
from .models import EvidenceResponse
from .progress import report, tracked
from .search import SearchUnavailable, search

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
        match = re.search(
            r"/(?:dp|gp/product)/([A-Z0-9]{10})(?:/|$)", parsed.path, re.IGNORECASE
        )
        return "https://www.amazon.com/dp/" + match[1].upper() if match else None
    except ValueError:
        return None


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
    try:
        sources, meta = await search(f"site:amazon.com {name}")
    except SearchUnavailable:
        return BuyResponse(
            product=name,
            status="needs_listing",
            data_source="unavailable",
            error="Amazon listing search unavailable",
            summary="Search is unavailable. Paste the Amazon product link to prepare a cart addition.",
        )
    candidates = {}
    for source in sources:
        url = amazon_listing(source.url)
        if url:
            candidates[url] = {"title": source.title, "url": url}
    return BuyResponse(
        product=name,
        quantity=req.quantity,
        status="choose_listing",
        candidates=list(candidates.values())[:5],
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


def build_buyer_agent(port=None):
    agent = make_agent(
        "focal-amazon-buyer-demo",
        os.getenv("BUYER_AGENT_SEED", "laai buyer demo seed"),
        port,
    )

    @agent.on_rest_post("/buy/prepare", BuyRequest, BuyResponse)
    async def on_prepare(ctx, req):
        return await prepare(req)

    @agent.on_rest_post("/buy/confirm", BuyConfirmation, BuyResponse)
    async def on_confirm(ctx, req):
        return await confirm(req)

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
