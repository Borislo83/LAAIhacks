"""The specialist uAgents: shopping research, YouTube, news and messaging.

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
    NewsRequest,
    NewsResponse,
    PriceCompareRequest,
    PriceCompareResponse,
    ReviewsRequest,
    ReviewsResponse,
    YouTubeRequest,
    YouTubeResponse,
)

# Seeds fix each agent's address. Override them in .env before deploying anywhere public.
PRICE_SEED = os.getenv("PRICE_AGENT_SEED", "laai price compare agent dev seed")
ALTERNATIVES_SEED = os.getenv("ALTERNATIVES_AGENT_SEED", "laai alternatives agent dev seed")
REVIEWS_SEED = os.getenv("REVIEWS_AGENT_SEED", "laai reviews agent dev seed")
YOUTUBE_SEED = os.getenv("YOUTUBE_AGENT_SEED", "laai youtube agent dev seed")
MESSENGER_SEED = os.getenv("MESSENGER_AGENT_SEED", "laai messenger agent dev seed")
COORDINATOR_SEED = os.getenv("COORDINATOR_AGENT_SEED", "laai coordinator dev seed")


def coordinator_address() -> str:
    """Action agents accept approvals only from this address."""
    from uagents.crypto import Identity

    return Identity.from_seed(COORDINATOR_SEED, 0).address
NEWS_SEED = os.getenv("NEWS_AGENT_SEED", "laai news agent dev seed")


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
    chat_request: Callable[[str], Model] | None = None,
    chat_reply: Callable[[Model], str] | None = None,
) -> Agent:
    """One specialist: typed protocol, REST endpoint and chat, all served by `handler`.

    Chat extracts a product name by default; `chat_request` builds the request from the
    raw message text instead. Chat replies with the response's `summary` unless
    `chat_reply` formats the response.
    """
    agent = make_agent(name, seed, port)
    proto = Protocol(name=request_type.__name__.removesuffix("Request"), version="0.1.0")

    @proto.on_message(request_type, replies=response_type)
    async def on_request(ctx: Context, sender: str, req):
        subject = getattr(req, "product", None) or getattr(req, "query", None)
        ctx.logger.info(f"{name} request for {subject!r} from {sender}")
        await ctx.send(sender, await handler(req))

    @agent.on_rest_post(route, request_type, response_type)
    async def on_rest(ctx: Context, req):
        return await handler(req)

    async def answer(ctx: Context, text: str) -> str:
        request = chat_request(text) if chat_request else request_type(product=text)
        result = await handler(request)
        return chat_reply(result) if chat_reply else result.summary

    agent.include(proto, publish_manifest=True)
    agent.include(
        build_chat_protocol(answer, extract_product=chat_request is None),
        publish_manifest=True,
    )
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


def build_youtube_agent(port: int | None = None) -> Agent:
    from .youtube import find_video

    return build_specialist(
        "laai-youtube",
        YOUTUBE_SEED,
        "/youtube",
        YouTubeRequest,
        YouTubeResponse,
        find_video,
        port,
        chat_request=lambda text: YouTubeRequest(query=text[:300]),
    )


def build_news_agent(port: int | None = None) -> Agent:
    from . import news

    return build_specialist(
        "laai-news",
        NEWS_SEED,
        "/news",
        NewsRequest,
        NewsResponse,
        news.summarize_news,
        port,
        chat_request=lambda text: NewsRequest(query=text[:300]),
        chat_reply=news.chat_reply,
    )


def build_messenger_agent(port: int | None = None) -> Agent:
    """Typed protocol and chat only prepare drafts; sending needs the executor token."""
    from . import imessage

    agent = build_specialist(
        "laai-messenger",
        MESSENGER_SEED,
        "/imessage/prepare",
        imessage.MessageDraftRequest,
        imessage.MessageDraftResponse,
        imessage.prepare,
        port,
        chat_request=lambda text: imessage.MessageDraftRequest(request=text[:1200]),
    )

    @agent.on_rest_post("/imessage/send", imessage.MessageSendRequest, imessage.MessageSendResponse)
    async def on_send(ctx: Context, req):
        return await imessage.send(req)

    # The coordinator relays a draft the shopper approved in ASI:One. Only the
    # coordinator's address is trusted; the executor token never leaves this process.
    approvals = Protocol(name="MessageApproval", version="0.1.0")

    @approvals.on_message(imessage.MessageApproval, replies=imessage.MessageSendResponse)
    async def on_approval(ctx: Context, sender: str, req):
        if sender != coordinator_address():
            await ctx.send(
                sender,
                imessage.MessageSendResponse(status="failed", summary="Only the coordinator can approve sends."),
            )
            return
        await ctx.send(
            sender,
            await imessage.send(
                imessage.MessageSendRequest(
                    draft_id=req.draft_id,
                    text=req.text,
                    executor_token=os.getenv("IMESSAGE_EXECUTOR_TOKEN", ""),
                )
            ),
        )

    agent.include(approvals, publish_manifest=True)

    # Dry-run debugging: REST handlers cannot see the caller's address, so this is
    # gated by the executor token instead of loopback, and is off when sending for real.
    @agent.on_rest_post(
        "/imessage/dry-run-log", imessage.DryRunLogRequest, imessage.DryRunLogResponse
    )
    async def on_dry_run_log(ctx: Context, req):
        return imessage.read_dry_run_log(req)

    return agent
