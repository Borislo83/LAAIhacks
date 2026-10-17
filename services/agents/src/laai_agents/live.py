"""Live search evidence: broad price candidates from any store, ranked by trust."""

import asyncio
import re
from urllib.parse import quote_plus

from . import catalog
from .models import (
    Alternative,
    AlternativesResponse,
    PriceCompareRequest,
    PriceCompareResponse,
    ReviewsResponse,
)
from .search import SearchUnavailable, search


def product_name(query: str) -> str:
    known = catalog.find_product(query)
    return known.name if known else query


def matches_product(title: str, product: str) -> bool:
    tokens = set(re.findall(r"[a-z0-9]+", title.lower()))
    known = catalog.find_product(product)
    names = [product, *([known.name, *known.aliases] if known else [])]
    return any(
        len(words := re.findall(r"[a-z0-9]+", name.lower())) >= 2
        and set(words) <= tokens
        for name in names
    )


async def _candidates(sources, name):
    from .merchants import fetch_offers, snippet_offer

    leads = [s for s in sources if matches_product(f"{s.title} {s.snippet}", name)]
    urls = list(dict.fromkeys(s.url for s in leads))[:5]
    page_offers = [
        offer
        for group in await asyncio.gather(*(fetch_offers(url, name) for url in urls))
        for offer in group
    ]
    read = {o.url for o in page_offers}
    return page_offers + [
        offer
        for s in leads
        if s.url not in read and (offer := snippet_offer(s, name))
    ]


async def compare(req):
    from .merchants import best_offer, rank

    name = product_name(req.product)
    # Stage 1: every result naming the product is a candidate, from any store.
    sources, meta = await search(f"{name} price USD")
    offers = await _candidates(sources, name)
    if not offers:
        # A second, store-focused query widens the pool only when the first found nothing.
        try:
            more, _ = await search(f"buy {name} online store")
        except SearchUnavailable:
            more = []
        seen = {s.url for s in sources}
        more = [s for s in more if s.url not in seen]
        sources += more
        offers = await _candidates(more, name)
    meta["sources"] = sources
    # Stage 2: rank by trust, then price; only clear mismatches were dropped.
    offers = rank(offers)
    best = best_offer(offers)
    if best is None:
        summary = (
            f"Found sources for {name}, but none quoted a clear USD price. "
            "Open the sources to check current offers."
        )
    elif best.trust == "low":
        summary = (
            f"Only unverified price leads found: lowest ${best.price:.2f} at {best.store}. "
            "Confirm on the store page before relying on it."
        )
    else:
        where = (
            "read from the product page"
            if best.source != "search_snippet"
            else "from the search listing"
        )
        summary = (
            f"Lowest {best.trust}-trust price: ${best.price:.2f} at {best.store} ({where}). "
            "Verify the listing before buying."
        )
    return PriceCompareResponse(
        product=name, offers=offers, best=best, savings=None, summary=summary, **meta
    )


async def alternatives(req):
    name = product_name(req.product)
    # Category identity only; prices/reviews never come from the fixture catalog.
    known = catalog.find_product(name)
    category = known.category if known else "products in the same category"
    budget = (
        f"under ${req.max_price:.2f}" if req.max_price is not None else "lower priced"
    )
    sources, meta = await search(f"{name} alternatives {category} {budget} USD")
    candidates = [
        p
        for p in catalog.PRODUCTS
        if known and p.category == known.category and p.name != known.name
    ]
    # Known names/category are seeds only. Each candidate needs its own live price evidence.
    results = await asyncio.gather(
        *(compare(PriceCompareRequest(product=p.name)) for p in candidates[:3]),
        return_exceptions=True,
    )
    priced, unpriced = [], []
    for product, result in zip(candidates, results):
        best = None if isinstance(result, Exception) else result.best
        if best is None:
            # Stores often block automated reads; keep the candidate, flag the price.
            unpriced.append(
                Alternative(
                    product=product.name,
                    price=None,
                    url=_research_url(product.name),
                    reason="Same category; no store price could be read, check it yourself",
                )
            )
            continue
        if req.max_price is not None and best.price >= req.max_price:
            continue
        priced.append(
            Alternative(
                product=best.product,
                price=best.price,
                url=best.url,
                reason=f"{best.trust.capitalize()}-trust price from {best.store}; verify listing",
            )
        )
        sources.extend(result.sources)
    priced.sort(key=lambda a: a.price)
    options = priced + unpriced
    if priced:
        summary = (
            f"Candidate {budget}: {priced[0].product} at ${priced[0].price:.2f}. "
            "Check availability and final checkout price at the source."
        )
    elif unpriced:
        summary = (
            f"Alternatives to consider: {', '.join(a.product for a in unpriced)}. "
            f"Stores didn't share prices with the search, so check each is {budget}."
        )
    else:
        summary = (
            f"Found alternative research sources {budget}, but no known products in the "
            "same category; review the linked sources before choosing."
        )
    meta["sources"] = list({s.url: s for s in sources}.values())[:20]
    return AlternativesResponse(
        product=name,
        alternatives=options,
        summary=summary,
        **meta,
    )


def _research_url(product: str) -> str:
    return "https://duckduckgo.com/?q=" + quote_plus(f"{product} price")


async def reviews(req):
    name = product_name(req.product)
    sources, meta = await search(f"{name} independent review")
    excerpts = [f"{s.title}: {s.snippet[:280]}" for s in sources if s.snippet][:2]
    summary = (
        "Review search excerpts (not full-article analysis): " + " ".join(excerpts)
        if excerpts
        else f"Found review sources for {name}."
    )
    return ReviewsResponse(
        product=name, review_url=sources[0].url, summary=summary, **meta
    )
