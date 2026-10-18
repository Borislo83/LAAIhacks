"""Live search evidence: broad price candidates from any store, ranked by trust."""

import asyncio
import re
from functools import lru_cache
from urllib.parse import quote_plus

from . import catalog
from .models import (
    Alternative,
    AlternativesResponse,
    PriceCompareRequest,
    PriceCompareResponse,
    ReviewsResponse,
)
from .progress import report
from .search import SearchUnavailable, search


def product_name(query: str) -> str:
    known = catalog.find_product(query)
    return known.name if known else query


@lru_cache(maxsize=512)
def _name_words(product: str) -> tuple[frozenset[str], ...]:
    """Word sets for the product and its catalog names, ignoring one-word names."""
    known = catalog.find_product(product)
    names = [product, *([known.name, *known.aliases] if known else [])]
    return tuple(
        frozenset(words)
        for name in names
        if len(words := re.findall(r"[a-z0-9]+", name.lower())) >= 2
    )


def matches_product(title: str, product: str) -> bool:
    tokens = set(re.findall(r"[a-z0-9]+", title.lower()))
    return any(words <= tokens for words in _name_words(product))


async def _candidates(sources, name):
    from .merchants import fetch_offers, snippet_offer

    leads = [s for s in sources if matches_product(f"{s.title} {s.snippet}", name)]
    urls = list(dict.fromkeys(s.url for s in leads))[:5]
    report("validating", f"Checking offers on {len(urls)} merchant pages and ranking evidence")
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


STORE_SOURCES = {"bestbuy_api", "serpapi_amazon", "serpapi_shopping"}


async def store_listings(name: str, budget: float | None = None):
    """Official store listings for `name` within budget, cheapest first."""
    from .merchants import WRONG_ITEM
    from .stores import store_offers

    offers = await store_offers(name, budget)
    # A model-specific name ("Sony WH-1000XM5") must match; a one-word one can't be checked.
    specific = len(re.findall(r"[a-z0-9]+", name.lower())) >= 2
    keep = [
        o
        for o in offers
        if not WRONG_ITEM.search(o.product) and (not specific or matches_product(o.product, name))
    ]
    if keep:
        report("store_listings", f"Found {len(keep)} store listings from official sources")
    return sorted(keep, key=lambda o: o.price)


async def compare(req):
    from .merchants import best_offer, rank

    name = product_name(req.product)
    budget = getattr(req, "max_price", None)
    # Official store listings (when API keys are set) run alongside the web search.
    listings_task = asyncio.create_task(store_listings(name, budget))
    # Stage 1: every result naming the product is a candidate, from any store.
    try:
        sources, meta = await search(f"{name} price USD")
    except SearchUnavailable:
        listings = await listings_task
        if not listings:
            raise
        sources, meta = [], {"data_source": "store_api", "sources": [], "notice": None}
    except BaseException:
        listings_task.cancel()
        raise
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
    listings = await listings_task
    if listings:
        meta["data_source"] = "store_api+" + meta.get("data_source", "web")
    offers = listings + offers
    best = best_offer(offers)
    if best is not None and best.source in STORE_SOURCES:
        within = f" under ${budget:.2f}" if budget is not None else ""
        summary = (
            f"Best price{within}: {best.product} at ${best.price:.2f} on {best.store} "
            f"({len(listings)} store listings checked)."
        )
    elif best is None:
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


async def store_alternatives(name, known, max_price):
    """Other brands in the same category from store listings, best rated first."""
    from .merchants import WRONG_ITEM
    from .stores import has_store_keys, store_offers

    if not has_store_keys():
        return []
    words = re.findall(r"[A-Za-z0-9]+", name)
    # "Sony headphones" -> "headphones"; a known product uses its catalog category.
    query = known.category if known else " ".join(words[1:]) or name
    brand = (known.name if known else name).split()[0].lower()
    offers = await store_offers(query, max_price)
    picked = [
        o
        for o in offers
        if not WRONG_ITEM.search(o.product)
        and brand not in o.product.lower()
        and not matches_product(o.product, name)
    ]
    picked.sort(key=lambda o: (-(o.rating or 0), -(o.review_count or 0), o.price))
    return [
        Alternative(
            product=o.product,
            price=o.price,
            rating=o.rating,
            url=o.url,
            store=o.store,
            cart_url=o.cart_url,
            reason=(f"{o.rating}★ on {o.store}" if o.rating else f"Listed on {o.store}")
            + (f", under ${max_price:.2f}" if max_price is not None else ""),
        )
        for o in picked[:3]
    ]


async def alternatives(req):
    name = product_name(req.product)
    # Category identity only; prices/reviews never come from the fixture catalog.
    known = catalog.find_product(name)
    category = known.category if known else "products in the same category"
    budget = (
        f"under ${req.max_price:.2f}" if req.max_price is not None else "lower priced"
    )
    candidates = [
        p
        for p in catalog.PRODUCTS
        if known and p.category == known.category and p.name != known.name
    ][:3]
    # Known names/category are seeds only. Each candidate needs its own live price
    # evidence; the category search runs alongside those comparisons.
    found, *results = await asyncio.gather(
        search(f"{name} alternatives {category} {budget} USD"),
        *(
            compare(PriceCompareRequest(product=p.name, max_price=req.max_price))
            for p in candidates
        ),
        return_exceptions=True,
    )
    if isinstance(found, BaseException):
        raise found
    sources, meta = found
    report("comparing", f"Comparing {len(results)} candidates against the budget")
    # Store-search alternatives are always priced; catalog leads may not be.
    priced, unpriced = await store_alternatives(name, known, req.max_price), []
    for product, result in zip(candidates, results):
        best = None if isinstance(result, BaseException) else result.best
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
                rating=best.rating,
                url=best.url,
                store=best.store,
                cart_url=best.cart_url,
                reason=f"{best.trust.capitalize()}-trust price from {best.store}; verify listing",
            )
        )
        sources.extend(result.sources)
    # Catalog leads without a price stay last.
    priced = list({a.product.lower(): a for a in reversed(priced)}.values())
    priced.sort(key=lambda a: (-(a.rating or 0), a.price))
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


# Notes that say the same thing in different words ("Expensive", "Price", "Pricey").
SAME_NOTE = [
    re.compile(r"\b(?:expensive|price[ds]?|pricey|costly|cost)\b", re.IGNORECASE),
    re.compile(r"\bbattery\b", re.IGNORECASE),
    re.compile(r"\b(?:anc|noise[- ]cancell?(?:ing|ation))\b", re.IGNORECASE),
]


def merge_notes(notes: list[str], new: list[str]) -> list[str]:
    """Add notes that aren't already covered, by wording or by a shared short topic."""
    merged = list(notes)
    for note in new:
        key = note.lower().strip(" .")
        if any(key == n.lower().strip(" .") for n in merged):
            continue
        # A short note ("Price") adds nothing next to one on the same topic.
        topic = next((t for t in SAME_NOTE if t.search(note)), None)
        if topic and len(note.split()) <= 3 and any(topic.search(n) for n in merged):
            continue
        merged.append(note)
    return merged


async def reviews(req):
    from .expert_reviews import parse_review, site_name
    from .merchants import MALFORMED, fetch_page

    name = product_name(req.product)
    sources, meta = await search(f"{name} independent review")
    leads = [s for s in sources if matches_product(f"{s.title} {s.snippet}", name)]
    urls = list(dict.fromkeys(s.url for s in leads or sources))[:6]
    report("reading", f"Reading {len(urls)} expert review pages")
    pages = await asyncio.gather(*(fetch_page(url) for url in urls))
    ratings, pros, cons = [], [], []
    for url, page in zip(urls, pages):
        try:
            rating, good, bad = parse_review(page, url, name) if page else (None, [], [])
        except MALFORMED:
            continue
        # One score per site: a review page and a product page can both carry it.
        if rating and rating.source not in {r.source for r in ratings}:
            ratings.append(rating)
            pros = merge_notes(pros, good)
            cons = merge_notes(cons, bad)
    report("summarizing", f"Combining {len(ratings)} expert ratings")
    average = round(sum(r.rating for r in ratings) / len(ratings), 1) if ratings else None
    if ratings:
        scores = ", ".join(f"{r.source} {r.rating:g}" for r in ratings)
        summary = f"Experts rate {name} {average}/5 on average ({scores})."
        if pros:
            summary += f" Pros: {pros[0]}."
        if cons:
            summary += f" Cons: {cons[0]}."
    else:
        names = ", ".join(dict.fromkeys(site_name(s.url) for s in sources[:5]))
        summary = (
            f"Found review sources for {name} ({names}), but none published a "
            "machine-readable rating. Open them to read the reviews."
        )
    best = max(ratings, key=lambda r: r.rating, default=None)
    return ReviewsResponse(
        product=name,
        rating=average,
        review_count=len(ratings),
        ratings=ratings,
        pros=pros[:4],
        cons=cons[:4],
        review_url=best.url if best else sources[0].url,
        summary=summary,
        **meta,
    )
