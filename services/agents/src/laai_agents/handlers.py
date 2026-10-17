"""Agent logic as plain async functions, independent of the uAgents runtime."""

from . import catalog, llm
from .models import (
    Alternative,
    AlternativesRequest,
    AlternativesResponse,
    Offer,
    PriceCompareRequest,
    PriceCompareResponse,
    ReviewsRequest,
    ReviewsResponse,
)


def _not_found(product: str) -> str:
    return f"I couldn't find '{product}' in the catalog."


async def compare_prices(req: PriceCompareRequest) -> PriceCompareResponse:
    product = catalog.find_product(req.product)
    if product is None:
        return PriceCompareResponse(product=req.product, offers=[], summary=_not_found(req.product))

    offers = sorted(
        (
            Offer(product=product.name, store=store, price=price, url=catalog.offer_url(store, product))
            for store, price in product.offers.items()
        ),
        key=lambda o: o.price,
    )
    best = offers[0]
    current = req.current_price
    if current is None and req.current_store in product.offers:
        current = product.offers[req.current_store]
    savings = round(current - best.price, 2) if current is not None else None

    if savings is not None and savings > 0:
        summary = f"{best.store} has {product.name} for ${best.price:.2f}, ${savings:.2f} less than here."
    elif savings is not None:
        summary = f"You're already looking at the best price for {product.name} (${best.price:.2f})."
    else:
        summary = f"Best price for {product.name} is ${best.price:.2f} at {best.store}."
    return PriceCompareResponse(
        product=product.name, offers=offers, best=best, savings=savings, summary=summary
    )


async def find_alternatives(req: AlternativesRequest) -> AlternativesResponse:
    product = catalog.find_product(req.product)
    if product is None:
        return AlternativesResponse(product=req.product, alternatives=[], summary=_not_found(req.product))

    base_price = min(product.offers.values())
    alternatives = []
    for alt in catalog.alternatives_for(product, req.max_price):
        store, price = min(alt.offers.items(), key=lambda kv: kv[1])
        reasons = []
        if price < base_price:
            reasons.append(f"${base_price - price:.2f} cheaper")
        if alt.rating > product.rating:
            reasons.append(f"rated {alt.rating} vs {product.rating}")
        alternatives.append(
            Alternative(
                product=alt.name,
                price=price,
                rating=alt.rating,
                url=catalog.offer_url(store, alt),
                reason=", ".join(reasons),
            )
        )

    if alternatives:
        top = alternatives[0]
        summary = f"Consider {top.product} (${top.price:.2f}): {top.reason}."
    else:
        summary = f"{product.name} is already the best value in its category."
    return AlternativesResponse(product=product.name, alternatives=alternatives, summary=summary)


async def get_reviews(req: ReviewsRequest) -> ReviewsResponse:
    product = catalog.find_product(req.product)
    if product is None:
        return ReviewsResponse(product=req.product, summary=_not_found(req.product))

    fallback = (
        f"{product.name}: {product.rating}/5 from {product.review_count:,} reviews. "
        f"Pros: {', '.join(product.pros)}. Cons: {', '.join(product.cons)}."
    )
    summary = await llm.complete(
        "You write one-sentence product review summaries for a smart-glasses display. "
        "Be concrete and under 30 words.",
        fallback,
    )
    return ReviewsResponse(
        product=product.name,
        rating=product.rating,
        review_count=product.review_count,
        pros=product.pros,
        cons=product.cons,
        review_url=catalog.review_url(product),
        summary=summary or fallback,
    )
