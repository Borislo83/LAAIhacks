"""Amazon and Best Buy cart agents: official add-to-cart links, opened by the user.

No browser automation and no account access: each agent validates a product ID and
returns the store's own add-to-cart URL. The item is added only when the user opens
that link in their browser (Amazon then asks them to confirm the addition).
"""

import os
import re
from urllib.parse import urlencode

from uagents import Agent, Context, Protocol

from .agents import make_agent
from .models import CartLink, CartRequest

ASIN = re.compile(r"[A-Z0-9]{10}")
SKU = re.compile(r"\d{4,9}")


def amazon_cart_link(asin: str, quantity: int = 1) -> str | None:
    """Amazon's documented "Add to Cart form" URL for one ASIN."""
    asin = (asin or "").strip().upper()
    if not ASIN.fullmatch(asin) or not 1 <= quantity <= 10:
        return None
    query = urlencode({"ASIN.1": asin, "Quantity.1": quantity})
    return f"https://www.amazon.com/gp/aws/cart/add.html?{query}"


def bestbuy_cart_link(sku: str | int) -> str | None:
    """Best Buy's add-to-cart URL for one SKU (the Products API's addToCartUrl)."""
    sku = str(sku or "").strip()
    if not SKU.fullmatch(sku):
        return None
    return f"https://api.bestbuy.com/click/-/{sku}/cart"


def _response(store: str, req: CartRequest, url: str | None) -> CartLink:
    name = "Amazon" if store == "amazon" else "Best Buy"
    if url is None:
        return CartLink(
            store=store,
            product_id=req.product_id,
            quantity=req.quantity,
            error=f"Not a valid {name} product ID",
            summary=f"Could not build a {name} cart link for that product ID.",
        )
    return CartLink(
        store=store,
        product_id=req.product_id,
        quantity=req.quantity,
        cart_url=url,
        summary=f"Open this link to add the item to your {name} cart. Nothing is purchased.",
    )


def build_amazon_cart_agent(port: int | None = None) -> Agent:
    agent = make_agent(
        "laai-amazon-cart", os.getenv("AMAZON_CART_AGENT_SEED", "laai amazon cart seed"), port
    )

    @agent.on_rest_post("/cart/amazon", CartRequest, CartLink)
    async def on_cart(ctx: Context, req: CartRequest) -> CartLink:
        return _response("amazon", req, amazon_cart_link(req.product_id, req.quantity))

    # Agent-to-agent: the coordinator asks for a link once the shopper approves.
    proto = Protocol(name="CartLink", version="0.1.0")

    @proto.on_message(CartRequest, replies=CartLink)
    async def on_request(ctx: Context, sender: str, req: CartRequest):
        await ctx.send(sender, _response("amazon", req, amazon_cart_link(req.product_id, req.quantity)))

    agent.include(proto, publish_manifest=True)
    return agent


def build_bestbuy_cart_agent(port: int | None = None) -> Agent:
    agent = make_agent(
        "laai-bestbuy-cart", os.getenv("BESTBUY_CART_AGENT_SEED", "laai bestbuy cart seed"), port
    )

    @agent.on_rest_post("/cart/bestbuy", CartRequest, CartLink)
    async def on_cart(ctx: Context, req: CartRequest) -> CartLink:
        return _response("bestbuy", req, bestbuy_cart_link(req.product_id))

    # Agent-to-agent: the coordinator asks for a link once the shopper approves.
    proto = Protocol(name="CartLink", version="0.1.0")

    @proto.on_message(CartRequest, replies=CartLink)
    async def on_request(ctx: Context, sender: str, req: CartRequest):
        await ctx.send(sender, _response("bestbuy", req, bestbuy_cart_link(req.product_id)))

    agent.include(proto, publish_manifest=True)
    return agent
