"""A client agent that sends one request to each agent and logs the replies."""

from uagents import Agent, Context
from uagents_core.contrib.protocols.chat import ChatAcknowledgement, ChatMessage

from .chat import message_text, text_message
from .models import (
    AlternativesRequest,
    AlternativesResponse,
    PriceCompareRequest,
    PriceCompareResponse,
    ReviewsRequest,
    ReviewsResponse,
)


def build_demo_client(addresses: dict[str, str]) -> Agent:
    client = Agent(name="laai-demo-client", seed="laai demo client dev seed")

    @client.on_event("startup")
    async def send_requests(ctx: Context):
        await ctx.send(
            addresses["laai-price-compare"],
            PriceCompareRequest(product="Sony WH-1000XM5", current_store="Best Buy"),
        )
        await ctx.send(addresses["laai-alternatives"], AlternativesRequest(product="Bose QC Ultra"))
        await ctx.send(addresses["laai-reviews"], ReviewsRequest(product="AirPods Pro 2"))
        await ctx.send(
            addresses["laai-reviews"],
            text_message("What do people think of the Nothing Ear (a)?", end_session=False),
        )

    @client.on_message(PriceCompareResponse)
    async def on_price(ctx: Context, sender: str, msg: PriceCompareResponse):
        ctx.logger.info(f"[price] {msg.summary}")

    @client.on_message(AlternativesResponse)
    async def on_alternatives(ctx: Context, sender: str, msg: AlternativesResponse):
        ctx.logger.info(f"[alternatives] {msg.summary}")

    @client.on_message(ReviewsResponse)
    async def on_reviews(ctx: Context, sender: str, msg: ReviewsResponse):
        ctx.logger.info(f"[reviews] {msg.summary}")

    @client.on_message(ChatMessage)
    async def on_chat(ctx: Context, sender: str, msg: ChatMessage):
        ctx.logger.info(f"[chat] {message_text(msg)}")

    @client.on_message(ChatAcknowledgement)
    async def on_ack(ctx: Context, sender: str, msg: ChatAcknowledgement):
        pass

    return client
