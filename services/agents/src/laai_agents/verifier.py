"""Verifier agent: audits the price agent's offers before anyone acts on them.

It keeps high- and medium-trust offers, drops low-trust leads, flags prices far from
the pack and offers over budget, and asks for a deeper search when nothing trustworthy
survives. It never invents a price; it only filters and explains.
"""

import os
from statistics import median

from pydantic.v1 import Field
from uagents import Model

from .models import Offer, PriceCompareResponse

OUTLIER_RATIO = 0.35


class VerifyRequest(Model):
    price: PriceCompareResponse
    max_price: float | None = None


class DroppedOffer(Model):
    store: str
    price: float
    reason: str


class VerifyResponse(Model):
    product: str
    kept: list[Offer] = Field(default_factory=list)
    dropped: list[DroppedOffer] = Field(default_factory=list)
    flags: list[str] = Field(default_factory=list)
    best: Offer | None = None
    within_budget: bool | None = None
    needs_retry: bool = False
    verdict: str
    summary: str


async def verify(req: VerifyRequest) -> VerifyResponse:
    price = req.price
    kept, dropped, flags = [], [], []
    for offer in price.offers:
        if offer.trust == "low":
            dropped.append(
                DroppedOffer(
                    store=offer.store,
                    price=offer.price,
                    reason="low trust: unknown store and no price on the page",
                )
            )
        else:
            kept.append(offer)
    if len(kept) >= 3:
        middle = median(o.price for o in kept)
        for offer in list(kept):
            if abs(offer.price - middle) / middle > OUTLIER_RATIO:
                kept.remove(offer)
                dropped.append(
                    DroppedOffer(
                        store=offer.store,
                        price=offer.price,
                        reason=f"outlier: {abs(offer.price - middle) / middle:.0%} from the median ${middle:,.2f}",
                    )
                )
    kept.sort(key=lambda o: (o.trust != "high", o.price))
    best = min(kept, key=lambda o: (o.trust != "high", o.price)) if kept else None
    if best and best.source == "search_snippet":
        flags.append(f"Best price at {best.store} comes from a search listing, not the product page")
    stores = {o.store for o in kept}
    if len(stores) == 1:
        flags.append("Only one store confirmed the price; no cross-check possible")
    within = None
    if req.max_price is not None and best is not None:
        within = best.price <= req.max_price
        if not within:
            flags.append(f"Best verified price {best.price:,.2f} is over the ${req.max_price:,.2f} budget")
    needs_retry = best is None
    if needs_retry:
        verdict = "rejected"
        summary = (
            f"Verifier rejected all {len(price.offers)} price leads for {price.product}; "
            "none came from a trusted store with the price on the page."
        )
    else:
        verdict = "verified" if best.trust == "high" and len(stores) > 1 else "caution"
        summary = (
            f"Verifier kept {len(kept)} of {len(price.offers)} offers across {len(stores)} "
            f"store(s); best verified ${best.price:,.2f} at {best.store} ({best.trust} trust)."
        )
    return VerifyResponse(
        product=price.product,
        kept=kept,
        dropped=dropped,
        flags=flags,
        best=best,
        within_budget=within,
        needs_retry=needs_retry,
        verdict=verdict,
        summary=summary,
    )


def build_verifier_agent(port: int | None = None):
    from .agents import build_specialist

    return build_specialist(
        "focal-price-verifier",
        os.getenv("VERIFIER_AGENT_SEED", "laai price verifier dev seed"),
        "/verify",
        VerifyRequest,
        VerifyResponse,
        verify,
        port,
        chat_request=lambda text: VerifyRequest(
            price=PriceCompareResponse(product=text[:200], offers=[], summary="")
        ),
    )
