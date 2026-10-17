"""Live search evidence: broad price candidates from any store, ranked by trust."""

import asyncio
import re

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


async def compare(req):
    from .merchants import best_offer, fetch_offers, rank, snippet_offer

    name = product_name(req.product)
    # Stage 1: two queries widen the pool; every result naming the product is a candidate.
    sources, meta = await search(f"{name} price USD")
    try:
        more, _ = await search(f"buy {name} online store")
    except SearchUnavailable:
        more = []
    seen = {s.url for s in sources}
    sources += [s for s in more if s.url not in seen]
    meta["sources"] = sources
    leads = [s for s in sources if matches_product(f"{s.title} {s.snippet}", name)]
    urls = list(dict.fromkeys(s.url for s in leads))[:8]
    page_offers = [
        offer
        for group in await asyncio.gather(*(fetch_offers(url, name) for url in urls))
        for offer in group
    ]
    read = {o.url for o in page_offers}
    snippet_offers = [
        offer
        for s in leads
        if s.url not in read and (offer := snippet_offer(s, name))
    ]
    # Stage 2: rank by trust, then price; only clear mismatches were dropped.
    offers = rank(page_offers + snippet_offers)
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
    options = []
    for result in results:
        if isinstance(result, Exception) or not result.best:
            continue
        best = result.best
        if req.max_price is not None and best.price >= req.max_price:
            continue
        options.append(
            Alternative(
                product=best.product,
                price=best.price,
                url=best.url,
                reason=f"{best.trust.capitalize()}-trust price from {best.store}; verify listing",
            )
        )
        sources.extend(result.sources)
    options.sort(key=lambda a: a.price)
    summary = (
        f"Candidate {budget}: {options[0].product} at ${options[0].price:.2f}. "
        "Check availability and final checkout price at the source."
        if options
        else f"Found alternative research sources {budget}. No matching alternative "
        "price was established; review the linked sources before choosing."
    )
    meta["sources"] = list({s.url: s for s in sources}.values())[:20]
    return AlternativesResponse(
        product=name,
        alternatives=options,
        summary=summary,
        **meta,
    )


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
