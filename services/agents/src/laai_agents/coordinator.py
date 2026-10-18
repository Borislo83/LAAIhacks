"""Focal coordinator: actual typed messages to three specialist uAgents."""

import asyncio
import re
import time
from collections import defaultdict
from collections.abc import Awaitable, Callable

from uagents import Context

from . import catalog, llm
from .agents import COORDINATOR_SEED, make_agent
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
from .progress import ProgressBatch, ProgressQuery, publish, read
from .verifier import VerifyRequest, VerifyResponse

VERIFIER = "focal-price-verifier"

ATTEMPTS = 3
RETRY_DELAY = 1.0
# Alternatives searches and price-checks up to three candidates, so it gets longer.
TIMEOUTS = {"laai-alternatives": 90}
DEFAULT_TIMEOUT = 45


def budget_from(text: str) -> float | None:
    match = re.search(
        r"(?:under|below|up to|at most|budget(?:\s+of)?)\s*\$?\s*(\d[\d,]*(?:\.\d{1,2})?)",
        text,
        re.IGNORECASE,
    )
    return float(match[1].replace(",", "")) if match else None


async def parse_request(text: str) -> ConsiderRequest | None:
    if re.fullmatch(
        r"(?:consider\s+)?(?:this|that)(?:\s+product)?[.!?]?",
        text.strip(),
        re.IGNORECASE,
    ):
        return None
    known = catalog.find_product(text)
    product = known.name if known else await llm.extract_product(text)
    return ConsiderRequest(product=product[:500], max_price=budget_from(text))


def compare_reviews(main: ReviewsResponse, alt: ReviewsResponse) -> str:
    def line(r: ReviewsResponse) -> str:
        if r.error:
            return f"{r.product}: reviews unavailable right now"
        if r.rating is not None:
            return f"{r.product}: {r.rating}/5 from {r.review_count} expert reviews"
        return f"{r.product}: {len(r.sources)} review sources, no published rating"

    verdict = ""
    if main.rating is not None and alt.rating is not None and main.rating != alt.rating:
        better = main if main.rating > alt.rating else alt
        verdict = f" Experts rate {better.product} higher."
    return f"Reviews compared: {line(main)} vs {line(alt)}.{verdict}"


# Called with each finished step so a chat can narrate the orchestration live.
Update = Callable[[str, object, int], Awaitable[None]]


async def consider(
    ctx: Context,
    addresses: dict[str, str],
    req: ConsiderRequest,
    on_update: Update | None = None,
) -> ConsiderResponse:
    specs = [
        (
            "price",
            "laai-price-compare",
            PriceCompareRequest(product=req.product, progress_id=req.progress_id),
            PriceCompareResponse,
        ),
        (
            "reviews",
            "laai-reviews",
            ReviewsRequest(product=req.product, progress_id=req.progress_id),
            ReviewsResponse,
        ),
        (
            "alternatives",
            "laai-alternatives",
            AlternativesRequest(
                product=req.product,
                max_price=req.max_price,
                progress_id=req.progress_id,
            ),
            AlternativesResponse,
        ),
    ]

    # uAgents matches a reply by (sender, destination, session), so two requests in
    # flight to the same specialist would steal each other's answer. One at a time each.
    locks = defaultdict(asyncio.Lock)

    async def delegate(spec):
        field, name, request, response_type = spec
        async with locks[name]:
            return await _delegate(field, name, request, response_type)

    async def _delegate(field, name, request, response_type):
        started = time.perf_counter()
        publish(
            req.progress_id, field, "delegated", "Coordinator sent work to specialist"
        )
        result = None
        # Retry only when no answer arrived. An answer carrying an error (search
        # unavailable) is final: asking again would just repeat the heavy work.
        for attempt in range(ATTEMPTS):
            if attempt:
                publish(
                    req.progress_id,
                    field,
                    "retrying",
                    f"No usable answer yet; retrying ({attempt + 1} of {ATTEMPTS})",
                )
                await asyncio.sleep(RETRY_DELAY * attempt)
            try:
                answer, _ = await ctx.send_and_receive(
                    addresses[name],
                    request,
                    response_type,
                    timeout=TIMEOUTS.get(name, DEFAULT_TIMEOUT),
                )
            except Exception:  # noqa: BLE001 -- isolate a failed specialist from the remaining work
                answer = None
            if isinstance(answer, response_type):
                result = answer
                break
        elapsed = round((time.perf_counter() - started) * 1000)
        if on_update:
            await on_update(field, result, elapsed)
        return field, result, elapsed

    async def alternatives_then_reviews():
        # Reviews for the top alternative start as soon as alternatives are known.
        first = await delegate(specs[2])
        top = first[1].alternatives[0].product if first[1] and first[1].alternatives else None
        if top is None:
            return [first]
        second = await delegate(
            (
                "alternative_reviews",
                "laai-reviews",
                ReviewsRequest(product=top, progress_id=req.progress_id),
                ReviewsResponse,
            )
        )
        return [first, second]

    *direct, chained = await asyncio.gather(
        delegate(specs[0]), delegate(specs[1]), alternatives_then_reviews()
    )
    finished = [*direct, *chained]
    price = direct[0][1]
    if price is not None and VERIFIER in addresses:
        audit = await delegate(
            (
                "verification",
                VERIFIER,
                VerifyRequest(price=price, max_price=req.max_price),
                VerifyResponse,
            )
        )
        if audit[1] is not None and audit[1].needs_retry and not price.error:
            # Recovery: nothing trustworthy survived, so ask for a deeper store search.
            retried = await delegate(
                (
                    "price_retry",
                    "laai-price-compare",
                    PriceCompareRequest(
                        product=req.product, progress_id=req.progress_id, deep=True
                    ),
                    PriceCompareResponse,
                )
            )
            if retried[1] is not None and not retried[1].error:
                direct[0] = ("price", retried[1], direct[0][2] + retried[2])
                audit = await delegate(
                    (
                        "verification",
                        VERIFIER,
                        VerifyRequest(price=retried[1], max_price=req.max_price),
                        VerifyResponse,
                    )
                )
        finished = [*direct, *chained, audit]
    values, errors, timings = {}, {}, {}
    for field, result, elapsed in finished:
        timings[field] = elapsed
        if result is None:
            errors[field] = "Specialist unavailable; no result was fabricated."
        else:
            values[field] = result
            if getattr(result, "error", None):
                errors[field] = result.error
    verification = values.pop("verification", None)
    sections = [
        result.summary for field, result in values.items() if field != "alternative_reviews"
    ]
    if verification is not None:
        sections.append(verification.summary)
    if "reviews" in values and "alternative_reviews" in values:
        sections.append(compare_reviews(values["reviews"], values["alternative_reviews"]))
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
        verification=verification.model_dump() if verification else None,
        **values,
    )


def build_coordinator(addresses: dict[str, str]):
    agent = make_agent("focal-shopping-assistant", COORDINATOR_SEED)

    @agent.on_rest_post("/consider", ConsiderRequest, ConsiderResponse)
    async def on_rest(ctx: Context, req: ConsiderRequest) -> ConsiderResponse:
        publish(
            req.progress_id, "coordinator", "planning", "Identifying product and budget"
        )
        parsed = await parse_request(req.product)
        if parsed is None:
            return ConsiderResponse(
                product=req.product, summary="Please include the product name."
            )
        if req.max_price is not None:
            parsed.max_price = req.max_price
        parsed.progress_id = req.progress_id
        return await consider(ctx, addresses, parsed)

    @agent.on_rest_post("/progress", ProgressQuery, ProgressBatch)
    async def progress(ctx: Context, req: ProgressQuery) -> ProgressBatch:
        return ProgressBatch(events=read(req.progress_id, req.after))

    # ASI:One gets the full narrated flow with Interactive Cards and the Payment Protocol.
    from .asi_flow import add_watch_loop, build_asi_protocols

    for protocol in build_asi_protocols(addresses):
        agent.include(protocol, publish_manifest=True)
    add_watch_loop(agent, addresses)
    return agent
