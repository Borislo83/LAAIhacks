"""Focal coordinator: actual typed messages to three specialist uAgents."""

import asyncio
import os
import re
import time

from uagents import Context

from . import catalog, llm
from .agents import make_agent
from .chat import build_chat_protocol
from .models import (
    AlternativesRequest,
    AlternativesResponse,
    ConsiderRequest,
    ConsiderResponse,
    Pick,
    PriceCompareRequest,
    PriceCompareResponse,
    ReviewsRequest,
    ReviewsResponse,
)
from .progress import ProgressBatch, ProgressQuery, publish, read

ATTEMPTS = 3
RETRY_DELAY = 1.0
# Alternatives searches and price-checks up to three candidates, so it gets longer.
TIMEOUTS = {"laai-alternatives": 90}
DEFAULT_TIMEOUT = 45


BUDGET = re.compile(
    r"(?:under|below|less than|up to|at most|max(?:imum)?|budget(?:\s+of)?)\s*\$?\s*\d[\d,]*(?:\.\d{1,2})?"
    r"(?:\s*(?:dollars|usd|bucks))?",
    re.IGNORECASE,
)
FILLER = re.compile(
    r"\b(?:consider|evaluate|assess|recommend|suggest|find|show|me|are|is|there|any|some|"
    r"good|best|cheap|cheaper|a|an|the|and|or|but|alternatives?|options?|please|i|i'm|im|"
    r"want|need|looking|for|to|buy|get|something|like|similar|what|which|can|could|you|"
    r"dollars|usd|bucks|in|my|range|price|budget|on|of|with|amazon|best\s*buy)\b",
    re.IGNORECASE,
)


def search_phrase(text: str) -> str:
    """The product words of a request, without budget, filler and punctuation."""
    text = BUDGET.sub(" ", text)
    text = re.sub(r"\$\s*\d[\d,.]*", " ", text)
    text = FILLER.sub(" ", re.sub(r"[^\w\s'+()-]", " ", text))
    return " ".join(text.split())[:120]


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
    if known:
        product = known.name
    elif llm.has_key():
        product = await llm.extract_product(text)
    else:
        # No LLM: "Are there Sony headphones under 300 dollars?" -> "Sony headphones".
        product = search_phrase(text) or text
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


TRUST_SCORE = {"high": 1.0, "medium": 0.6, "low": 0.2}


def make_picks(values: dict, max_price: float | None) -> list[Pick]:
    """Best price, an alternative, and a recommended balance of price, rating and trust."""
    price, alts = values.get("price"), values.get("alternatives")
    expert = {
        r.product.lower(): r.rating
        for r in (values.get("reviews"), values.get("alternative_reviews"))
        if r is not None and r.rating is not None
    }

    def in_budget(amount):
        return amount is not None and (max_price is None or amount < max_price)

    def rating_of(name, own):
        return own if own is not None else expert.get(name.lower())

    candidates = []
    for o in price.offers if price else []:
        if in_budget(o.price):
            candidates.append(
                dict(product=o.product, price=o.price, store=o.store, url=o.url,
                     cart_url=o.cart_url, rating=rating_of(o.product, o.rating), trust=o.trust,
                     origin="price")
            )
    for a in alts.alternatives if alts else []:
        if in_budget(a.price):
            candidates.append(
                dict(product=a.product, price=a.price, store=a.store, url=a.url,
                     cart_url=a.cart_url, rating=rating_of(a.product, a.rating),
                     trust="high" if a.cart_url else "medium", origin="alternative")
            )

    picks = []
    from_price = [c for c in candidates if c["origin"] == "price"]
    if from_price:
        best = min(from_price, key=lambda c: (-TRUST_SCORE[c["trust"]], c["price"]))
        within = f" under ${max_price:.2f}" if max_price is not None else ""
        picks.append(_pick("Best price", best, f"Lowest trusted price{within} on {best['store']}"))
    alternative = next(iter(alts.alternatives), None) if alts else None
    if alternative is not None:
        picks.append(
            Pick(label="Alternative", product=alternative.product, price=alternative.price,
                 store=alternative.store, url=alternative.url, cart_url=alternative.cart_url,
                 rating=rating_of(alternative.product, alternative.rating),
                 reason=alternative.reason)
        )
    if candidates:
        taken = {(p.product, p.price) for p in picks}
        pool = [c for c in candidates if (c["product"], c["price"]) not in taken] or candidates
        low = min(c["price"] for c in candidates)
        high = max(c["price"] for c in candidates)

        def score(c):
            rating = (c["rating"] or 3.0) / 5
            value = 1.0 if high == low else 1 - (c["price"] - low) / (high - low)
            return 0.45 * rating + 0.35 * value + 0.2 * TRUST_SCORE[c["trust"]]

        top = max(pool, key=score)
        stars = f"{top['rating']}★, " if top["rating"] else ""
        picks.append(
            _pick("Recommended", top, f"Best balance: {stars}${top['price']:.2f}, on {top['store']}")
        )
    return picks


def _pick(label: str, c: dict, reason: str) -> Pick:
    return Pick(label=label, product=c["product"], price=c["price"], store=c["store"],
                url=c["url"], cart_url=c["cart_url"], rating=c["rating"], reason=reason)


async def consider(
    ctx: Context, addresses: dict[str, str], req: ConsiderRequest
) -> ConsiderResponse:
    specs = [
        (
            "price",
            "laai-price-compare",
            PriceCompareRequest(
                product=req.product, progress_id=req.progress_id, max_price=req.max_price
            ),
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

    async def delegate(spec):
        field, name, request, response_type = spec
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
        return field, result, round((time.perf_counter() - started) * 1000)

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
    values, errors, timings = {}, {}, {}
    for field, result, elapsed in [*direct, *chained]:
        timings[field] = elapsed
        if result is None:
            errors[field] = "Specialist unavailable; no result was fabricated."
        else:
            values[field] = result
            if result.error:
                errors[field] = result.error
    sections = [
        result.summary for field, result in values.items() if field != "alternative_reviews"
    ]
    if "reviews" in values and "alternative_reviews" in values:
        sections.append(compare_reviews(values["reviews"], values["alternative_reviews"]))
    picks = make_picks(values, req.max_price)
    if picks:
        sections.insert(
            0,
            "Picks: "
            + "; ".join(
                f"{p.label}: {p.product}"
                + (f" (${p.price:.2f} on {p.store})" if p.price is not None else "")
                for p in picks
            )
            + ".",
        )
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
        picks=picks,
        **values,
    )


def build_coordinator(addresses: dict[str, str]):
    agent = make_agent(
        "focal-shopping-assistant",
        os.getenv("COORDINATOR_AGENT_SEED", "laai coordinator dev seed"),
    )

    async def answer(ctx: Context, text: str) -> str:
        req = await parse_request(text)
        if req is None:
            return "Which product should I consider? Include its name and optional budget."
        result = await consider(ctx, addresses, req)
        reply = result.summary
        for section in (result.price, result.reviews, result.alternatives):
            if section:
                reply += "\n" + "\n".join(
                    f"{s.title}: {s.url}" for s in section.sources[:3]
                )
        return reply

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

    agent.include(build_chat_protocol(answer, extract_product=False), publish_manifest=True)
    return agent
