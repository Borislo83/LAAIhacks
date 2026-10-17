"""Focal coordinator: actual typed messages to three specialist uAgents."""

import asyncio
import os
import re
import time
from datetime import UTC, datetime

from uagents import Context, Protocol
from uagents_core.contrib.protocols.chat import (
    ChatAcknowledgement,
    ChatMessage,
    TextContent,
    chat_protocol_spec,
)

from . import catalog, llm
from .agents import make_agent
from .chat import text_message
from .models import (
    AlternativesRequest,
    AlternativesResponse,
    ConsiderRequest,
    ConsiderResponse,
    PriceCompareRequest,
    PriceCompareResponse,
    ReviewsRequest,
    ReviewsResponse,
)


def budget_from(text: str) -> float | None:
    match = re.search(
        r"(?:under|below|up to|at most|budget(?:\s+of)?)\s*\$?\s*(\d[\d,]*(?:\.\d{1,2})?)",
        text,
        re.IGNORECASE,
    )
    return float(match[1].replace(",", "")) if match else None


async def parse_request(text: str) -> ConsiderRequest | None:
    known = catalog.find_product(text)
    product = known.name if known else await llm.extract_product(text)
    if re.fullmatch(
        r"(?:consider\s+)?(?:this|that)(?:\s+product)?[.!?]?",
        text.strip(),
        re.IGNORECASE,
    ):
        return None
    return ConsiderRequest(product=product[:500], max_price=budget_from(text))


async def consider(
    ctx: Context, addresses: dict[str, str], req: ConsiderRequest
) -> ConsiderResponse:
    specs = [
        (
            "price",
            "laai-price-compare",
            PriceCompareRequest(product=req.product),
            PriceCompareResponse,
        ),
        (
            "reviews",
            "laai-reviews",
            ReviewsRequest(product=req.product),
            ReviewsResponse,
        ),
        (
            "alternatives",
            "laai-alternatives",
            AlternativesRequest(product=req.product, max_price=req.max_price),
            AlternativesResponse,
        ),
    ]

    async def delegate(spec):
        field, name, request, response_type = spec
        started = time.perf_counter()
        try:
            result, _ = await ctx.send_and_receive(
                addresses[name],
                request,
                response_type,
                timeout=90,
            )
            return field, result, round((time.perf_counter() - started) * 1000)
        except Exception:  # noqa: BLE001 -- isolate a failed specialist from the remaining work
            return field, None, round((time.perf_counter() - started) * 1000)

    values, errors, timings = {}, {}, {}
    for field, result, elapsed in await asyncio.gather(*(delegate(s) for s in specs)):
        timings[field] = elapsed
        if result is None:
            errors[field] = "Specialist unavailable; no result was fabricated."
        else:
            values[field] = result
            if result.error:
                errors[field] = result.error
    sections = [result.summary for result in values.values()]
    sections.extend(f"{field}: {error}" for field, error in errors.items())
    if any(result.data_source == "demo_catalog" for result in values.values()):
        sections.insert(
            0, "DEMO: illustrative catalog data, not current offers or reviews."
        )
    return ConsiderResponse(
        product=req.product,
        max_price=req.max_price,
        errors=errors,
        timings_ms=timings,
        summary="\n\n".join(sections),
        **values,
    )


def build_coordinator(addresses: dict[str, str]):
    agent = make_agent(
        "focal-shopping-assistant",
        os.getenv("COORDINATOR_AGENT_SEED", "laai coordinator dev seed"),
    )
    proto = Protocol(spec=chat_protocol_spec)

    @proto.on_message(ChatMessage)
    async def on_chat(ctx: Context, sender: str, msg: ChatMessage):
        await ctx.send(
            sender,
            ChatAcknowledgement(
                timestamp=datetime.now(UTC),
                acknowledged_msg_id=msg.msg_id,
            ),
        )
        text = " ".join(
            c.text for c in msg.content if isinstance(c, TextContent)
        ).strip()
        if not text:
            return
        req = await parse_request(text)
        if req is None:
            answer = (
                "Which product should I consider? Include its name and optional budget."
            )
        else:
            result = await consider(ctx, addresses, req)
            answer = result.summary
            for section in (result.price, result.reviews, result.alternatives):
                if section:
                    answer += "\n" + "\n".join(
                        f"{s.title}: {s.url}" for s in section.sources[:3]
                    )
        await ctx.send(sender, text_message(answer))

    @proto.on_message(ChatAcknowledgement)
    async def on_ack(ctx: Context, sender: str, msg: ChatAcknowledgement):
        pass

    @agent.on_rest_post("/consider", ConsiderRequest, ConsiderResponse)
    async def on_rest(ctx: Context, req: ConsiderRequest) -> ConsiderResponse:
        parsed = await parse_request(req.product)
        if parsed is None:
            return ConsiderResponse(
                product=req.product, summary="Please include the product name."
            )
        if req.max_price is not None:
            parsed.max_price = req.max_price
        return await consider(ctx, addresses, parsed)

    agent.include(proto, publish_manifest=True)
    return agent
