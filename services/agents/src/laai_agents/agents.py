"""The three uAgents: price comparison, competitor alternatives, reviews.

Each agent speaks a typed request/response protocol (agent-to-agent), a REST
endpoint (for the orchestrator over plain HTTP) and the Agent Chat Protocol
(for ASI:One and Agentverse chat).
"""

import os

from uagents import Agent, Context, Protocol

from . import handlers
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
    return Agent(name=name, seed=seed, publish_agent_details=True, **kwargs)


def build_price_agent(port: int | None = None) -> Agent:
    agent = make_agent("laai-price-compare", PRICE_SEED, port)
    proto = Protocol(name="PriceCompare", version="0.1.0")

    @proto.on_message(PriceCompareRequest, replies=PriceCompareResponse)
    async def on_request(ctx: Context, sender: str, req: PriceCompareRequest):
        ctx.logger.info(f"Price compare for {req.product!r} from {sender}")
        await ctx.send(sender, await handlers.compare_prices(req))

    @agent.on_rest_post("/price", PriceCompareRequest, PriceCompareResponse)
    async def on_rest(ctx: Context, req: PriceCompareRequest) -> PriceCompareResponse:
        return await handlers.compare_prices(req)

    async def answer(product: str) -> str:
        return (await handlers.compare_prices(PriceCompareRequest(product=product))).summary

    agent.include(proto, publish_manifest=True)
    agent.include(build_chat_protocol(answer), publish_manifest=True)
    return agent


def build_alternatives_agent(port: int | None = None) -> Agent:
    agent = make_agent("laai-alternatives", ALTERNATIVES_SEED, port)
    proto = Protocol(name="Alternatives", version="0.1.0")

    @proto.on_message(AlternativesRequest, replies=AlternativesResponse)
    async def on_request(ctx: Context, sender: str, req: AlternativesRequest):
        ctx.logger.info(f"Alternatives for {req.product!r} from {sender}")
        await ctx.send(sender, await handlers.find_alternatives(req))

    @agent.on_rest_post("/alternatives", AlternativesRequest, AlternativesResponse)
    async def on_rest(ctx: Context, req: AlternativesRequest) -> AlternativesResponse:
        return await handlers.find_alternatives(req)

    async def answer(product: str) -> str:
        return (await handlers.find_alternatives(AlternativesRequest(product=product))).summary

    agent.include(proto, publish_manifest=True)
    agent.include(build_chat_protocol(answer), publish_manifest=True)
    return agent


def build_reviews_agent(port: int | None = None) -> Agent:
    agent = make_agent("laai-reviews", REVIEWS_SEED, port)
    proto = Protocol(name="Reviews", version="0.1.0")

    @proto.on_message(ReviewsRequest, replies=ReviewsResponse)
    async def on_request(ctx: Context, sender: str, req: ReviewsRequest):
        ctx.logger.info(f"Reviews for {req.product!r} from {sender}")
        await ctx.send(sender, await handlers.get_reviews(req))

    @agent.on_rest_post("/reviews", ReviewsRequest, ReviewsResponse)
    async def on_rest(ctx: Context, req: ReviewsRequest) -> ReviewsResponse:
        return await handlers.get_reviews(req)

    async def answer(product: str) -> str:
        return (await handlers.get_reviews(ReviewsRequest(product=product))).summary

    agent.include(proto, publish_manifest=True)
    agent.include(build_chat_protocol(answer), publish_manifest=True)
    return agent
