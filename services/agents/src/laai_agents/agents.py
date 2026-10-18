"""The three uAgents: price comparison, competitor alternatives, reviews.

Each agent speaks a typed request/response protocol (agent-to-agent), a REST
endpoint (for the orchestrator over plain HTTP) and the Agent Chat Protocol
(for ASI:One and Agentverse chat).
"""

import os
from collections.abc import Awaitable, Callable

from uagents import Agent, Context, Model, Protocol

from . import handlers, net
from .chat import build_chat_protocol
from .models import (
    AlternativesRequest,
    AlternativesResponse,
    PriceCompareRequest,
    PriceCompareResponse,
    ReviewsRequest,
    ReviewsResponse,
)

# Seeds fix each agent's address. Override them in .env before deploying anywhere public.
PRICE_SEED = os.getenv("PRICE_AGENT_SEED", "laai price compare agent dev seed")
ALTERNATIVES_SEED = os.getenv("ALTERNATIVES_AGENT_SEED", "laai alternatives agent dev seed")
REVIEWS_SEED = os.getenv("REVIEWS_AGENT_SEED", "laai reviews agent dev seed")


def make_agent(name: str, seed: str, port: int | None = None) -> Agent:
    kwargs = {}
    if port is not None:
        kwargs = {"port": port, "endpoint": [f"http://127.0.0.1:{port}/submit"]}
    # Set AGENTVERSE_MAILBOX=1 to reach the agent through Agentverse (needed for ASI:One).
    if os.getenv("AGENTVERSE_MAILBOX") == "1" or (
        name == "focal-shopping-assistant" and os.getenv("COORDINATOR_AGENTVERSE_MAILBOX") == "1"
    ):
        kwargs = {"port": port, "mailbox": True} if port else {"mailbox": True}
    agent = Agent(name=name, seed=seed, publish_agent_details=True, **kwargs)

    @agent.on_event("shutdown")
    async def close_http(ctx: Context):
        await net.close()

    return agent


def build_specialist(
    name: str,
    seed: str,
    route: str,
    request_type: type[Model],
    response_type: type[Model],
    handler: Callable[[Model], Awaitable[Model]],
    port: int | None = None,
) -> Agent:
    """One specialist: typed protocol, REST endpoint and chat, all served by `handler`."""
    agent = make_agent(name, seed, port)
    proto = Protocol(name=request_type.__name__.removesuffix("Request"), version="0.1.0")

    @proto.on_message(request_type, replies=response_type)
    async def on_request(ctx: Context, sender: str, req):
        ctx.logger.info(f"{name} request for {req.product!r} from {sender}")
        await ctx.send(sender, await handler(req))

    @agent.on_rest_post(route, request_type, response_type)
    async def on_rest(ctx: Context, req):
        return await handler(req)

    async def answer(ctx: Context, product: str) -> str:
        return (await handler(request_type(product=product))).summary

    agent.include(proto, publish_manifest=True)
    agent.include(build_chat_protocol(answer), publish_manifest=True)
    return agent


def build_price_agent(port: int | None = None) -> Agent:
    return build_specialist(
        "laai-price-compare",
        PRICE_SEED,
        "/price",
        PriceCompareRequest,
        PriceCompareResponse,
        handlers.compare_prices,
        port,
    )


def build_alternatives_agent(port: int | None = None) -> Agent:
    return build_specialist(
        "laai-alternatives",
        ALTERNATIVES_SEED,
        "/alternatives",
        AlternativesRequest,
        AlternativesResponse,
        handlers.find_alternatives,
        port,
    )


def build_reviews_agent(port: int | None = None) -> Agent:
    return build_specialist(
        "laai-reviews",
        REVIEWS_SEED,
        "/reviews",
        ReviewsRequest,
        ReviewsResponse,
        handlers.get_reviews,
        port,
    )
