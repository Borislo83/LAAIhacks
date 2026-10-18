"""Focal coordinator: actual typed messages to three specialist uAgents."""

import asyncio
import os
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
    Pick,
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


BUDGET = re.compile(
    r"(?:(?:under|below|less than|up to|at most|budget(?:\s+of)?)\s*"
    # Bare "max" is often a model name ("Pro Max 256GB"), so it needs a currency.
    r"|max(?:imum)?(?:\s+of)?\s*(?=\$|\d[\d,]*(?:\.\d{1,2})?\s*(?:dollars|usd|bucks)\b))"
    r"\$?\s*(\d[\d,]*(?:\.\d{1,2})?)(?:\s*(?:dollars|usd|bucks))?",
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


STORE = re.compile(
    # "I'm on Best Buy" is where the user is shopping now, not a store to limit to.
    r"(?<!i'm )(?<!im )(?<!i am )(?<!i’m )"
    r"\b(?:in|at|on|from|off)\s+(?:the\s+)?(amazon|best\s*buy|walmart|target|ebay|costco)"
    r"(?:\.com)?\b",
    re.IGNORECASE,
)
STORE_DOMAINS = {
    "amazon": "amazon.com", "bestbuy": "bestbuy.com", "walmart": "walmart.com",
    "target": "target.com", "ebay": "ebay.com", "costco": "costco.com",
}  # fmt: skip
# "Are there any smart watches under $50", "show me headphones on Amazon": a category to
# browse, not one product to research.
LISTING_ASK = re.compile(
    r"^\s*(?:(?:are|is)\s+there|any|show\s+me|list|find\s+(?:me\s+)?(?:some|any)|"
    r"what\s+(?:are\s+(?:the|some)|kind\s+of))\b",
    re.IGNORECASE,
)
CATEGORY_WORDS = {
    "smart", "watch", "watches", "smartwatch", "smartwatches", "fitness", "tracker", "trackers",
    "running", "headphones", "headphone", "earbuds", "earphones", "headset", "headsets",
    "wireless", "bluetooth", "noise", "cancelling", "canceling", "gaming", "phone", "phones",
    "smartphone", "smartphones", "laptop", "laptops", "tablet", "tablets", "tv", "tvs",
    "speaker", "speakers", "camera", "cameras", "keyboard", "keyboards", "mouse", "monitor",
    "monitors", "charger", "chargers", "kids", "men", "mens", "women", "womens", "portable",
    "over", "ear", "in", "for", "cheap", "budget", "good", "decent", "new", "4k", "usb", "c",
}  # fmt: skip


def store_from(text: str) -> str | None:
    match = STORE.search(text)
    return STORE_DOMAINS[re.sub(r"\s+", "", match[1].lower())] if match else None


def is_listing(text: str, product: str) -> bool:
    """A category ("smart watches"), or any ask to list a store's products, not one model."""
    if catalog.find_product(product) or re.search(r"\d", product):
        return False
    words = re.findall(r"[a-z0-9]+", product.lower())
    if not words:
        return False
    generic = all(w in CATEGORY_WORDS for w in words)
    return generic or bool(LISTING_ASK.search(text) and store_from(text))


def budget_from(text: str) -> float | None:
    match = BUDGET.search(text)
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
        # No LLM: "Are there Sony headphones under 300 dollars?" -> "Sony headphones";
        # "Is there a better alternative for the Garmin 55?" -> "Garmin 55".
        from .live import clean_query

        cleaned = clean_query(STORE.sub(" ", text))
        product = search_phrase(cleaned) or cleaned
    product = " ".join(STORE.sub(" ", product).split()) or product
    max_price = budget_from(text)
    store = store_from(text)
    return ConsiderRequest(
        product=product[:500],
        max_price=max_price,
        store=store,
        browse=is_listing(text, product) and (max_price is not None or store is not None),
    )


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
                     origin="price", image=o.image)
            )
    for a in alts.alternatives if alts else []:
        if in_budget(a.price):
            candidates.append(
                dict(product=a.product, price=a.price, store=a.store, url=a.url,
                     cart_url=a.cart_url, rating=rating_of(a.product, a.rating),
                     trust="high" if a.cart_url else "medium", origin="alternative",
                     image=a.image)
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
                 reason=alternative.reason, image=alternative.image)
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
                url=c["url"], cart_url=c["cart_url"], rating=c["rating"], reason=reason,
                image=c.get("image"))


async def with_images(product: str, picks: list[Pick]) -> tuple[str | None, list[Pick]]:
    """A photo of the product and of each pick, from Amazon listings (8 s at most)."""
    if os.getenv("PRODUCT_DATA_MODE", "live") != "live":
        return None, picks
    from .stores import product_image

    names = list(dict.fromkeys([product, *(p.product for p in picks if not p.image)]))[:4]

    async def one(name):
        try:
            return await asyncio.wait_for(product_image(name), timeout=8)
        except Exception:  # noqa: BLE001 -- a missing photo never blocks the answer
            return None

    found = dict(zip(names, await asyncio.gather(*(one(n) for n in names)), strict=True))
    return found.get(product), [
        p if p.image else p.copy(update={"image": found.get(p.product)}) for p in picks
    ]


# Called with each finished step so a chat can narrate the orchestration live.
Update = Callable[[str, object, int], Awaitable[None]]


async def consider(
    ctx: Context,
    addresses: dict[str, str],
    req: ConsiderRequest,
    on_update: Update | None = None,
) -> ConsiderResponse:
    if req.browse:
        return await browse(ctx, addresses, req, on_update)
    # A loose or misspelled product ("garmin 5 forerunning watch") is resolved once to
    # its real name ("Garmin Forerunner 55") so every specialist researches the same thing.
    if os.getenv("PRODUCT_DATA_MODE", "live") == "live":
        from .live import resolve_product

        try:
            resolved = await asyncio.wait_for(resolve_product(req.product), timeout=8)
        except (TimeoutError, ValueError):
            resolved = req.product
        if resolved != req.product:
            publish(req.progress_id, "coordinator", "identified", f"Researching {resolved}")
            req = req.copy(update={"product": resolved})
    specs = [
        (
            "price",
            "laai-price-compare",
            PriceCompareRequest(
                product=req.product, progress_id=req.progress_id, max_price=req.max_price,
                store=req.store,
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
                        product=req.product,
                        progress_id=req.progress_id,
                        max_price=req.max_price,
                        store=req.store,
                        deep=True,
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
    picks = make_picks(values, req.max_price)
    image, picks = await with_images(req.product, picks)
    if picks:
        # The picks and each agent's card carry the details, so the reply stays short:
        # the recommendation and, when both were reviewed, how the reviews compare.
        top = next((p for p in picks if p.label == "Recommended"), picks[0])
        price = f" at ${top.price:.2f} on {top.store}" if top.price is not None else ""
        sections = [f"My pick: {top.product}{price}."] + [
            s for s in sections[-1:] if s.startswith("Reviews compared:")
        ]
    else:
        sections.extend(f"{field}: {error}" for field, error in errors.items())
    if any(result.data_source == "demo_catalog" for result in values.values()):
        sections.insert(
            0, "DEMO: illustrative catalog data, not current offers or reviews."
        )
    return ConsiderResponse(
        product=req.product,
        max_price=req.max_price,
        store=req.store,
        image=image,
        errors=errors,
        timings_ms=timings,
        summary="\n\n".join(sections),
        picks=picks,
        verification=verification.model_dump() if verification else None,
        **values,
    )


def listing_picks(offers) -> list[Pick]:
    """Most popular, best rated (with enough ratings to mean something) and cheapest."""
    from .live import _popularity

    if not offers:
        return []
    rated = [o for o in offers if (o.review_count or 0) >= 100] or offers
    choices = [
        ("Most popular", max(offers, key=lambda o: o.review_count or 0)),
        ("Best rated", max(rated, key=lambda o: (o.rating or 0, o.review_count or 0))),
        ("Cheapest", min(offers, key=lambda o: o.price)),
        ("Also good", max(offers, key=_popularity)),
    ]
    picks, taken = [], set()
    for label, o in choices:
        if o.url in taken or len(picks) == 3:
            continue
        taken.add(o.url)
        stars = f"{o.rating}★" + (f" from {o.review_count:,} ratings" if o.review_count else "")
        picks.append(
            Pick(label=label, product=o.product, price=o.price, store=o.store, url=o.url,
                 cart_url=o.cart_url, rating=o.rating, image=o.image,
                 reason=f"{stars}, ${o.price:.2f} on {o.store}" if o.rating
                 else f"${o.price:.2f} on {o.store}")
        )  # fmt: skip
    return picks


async def browse(ctx, addresses, req: ConsiderRequest, on_update=None) -> ConsiderResponse:
    """A category in budget: the price agent reads the store's listings."""
    where = req.store or "Amazon and Best Buy"
    publish(req.progress_id, "coordinator", "identified", f"Listing {req.product} at {where}")
    started = time.perf_counter()
    request = PriceCompareRequest(
        product=req.product, progress_id=req.progress_id, max_price=req.max_price,
        store=req.store, browse=True,
    )  # fmt: skip
    publish(req.progress_id, "price", "delegated", "Coordinator sent work to specialist")
    price = None
    for attempt in range(ATTEMPTS):
        if attempt:
            await asyncio.sleep(RETRY_DELAY * attempt)
        try:
            answer, _ = await ctx.send_and_receive(
                addresses["laai-price-compare"], request, PriceCompareResponse,
                timeout=DEFAULT_TIMEOUT,
            )  # fmt: skip
        except Exception:  # noqa: BLE001 -- a failed specialist is reported, not raised
            answer = None
        if isinstance(answer, PriceCompareResponse):
            price = answer
            break
    elapsed = round((time.perf_counter() - started) * 1000)
    if on_update:
        await on_update("price", price, elapsed)
    if price is None:
        return ConsiderResponse(
            product=req.product, max_price=req.max_price, store=req.store, mode="browse",
            errors={"price": "Specialist unavailable; no result was fabricated."},
            timings_ms={"price": elapsed},
            summary="The price agent is unavailable right now. Try again in a moment.",
        )  # fmt: skip
    return ConsiderResponse(
        product=req.product, max_price=req.max_price, store=req.store, mode="browse",
        price=price, picks=listing_picks(price.offers),
        errors={"price": price.error} if price.error else {},
        timings_ms={"price": elapsed}, summary=price.summary,
    )  # fmt: skip


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
        if req.store:
            parsed.store = req.store
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
