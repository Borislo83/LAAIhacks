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


# Question phrasing around a product: "Is the Garmin Forerunner worth it or not?"
QUESTION_PARTS = [
    re.compile(p, re.IGNORECASE)
    for p in (
        r"^\s*(?:hey|ok|okay|so|please)[\s,]+",
        # "Is there a better alternative for X", "any cheaper options than X", typos included.
        r"^\s*(?:(?:is|are)\s+there\s+|what(?:'s|\s+is|\s+are)\s+|any\s+|find\s+(?:me\s+)?)?"
        r"(?:a|an|any|some|the)?\s*(?:better|cheaper|good|similar|best)?\s*"
        r"(?:alt[ae]r[an]+\w*|options?|choices?|replacements?|substitutes?|competitors?)\s+"
        r"(?:to|for|than|of)\s+",
        r"\s+(?:under|below|less\s+than|up\s+to|for\s+under)\s*\$?\s*\d[\d,]*(?:\.\d+)?"
        r"(?:\s*(?:dollars|usd|bucks))?\s*[?.!]*\s*$",
        r"^\s*(?:is|are|was|were|should\s+i\s+(?:buy|get)|would\s+you\s+recommend|"
        r"do\s+you\s+recommend|how\s+good\s+(?:is|are)|what\s+do\s+you\s+think\s+(?:of|about)|"
        r"tell\s+me\s+about|(?:show\s+me\s+|any\s+)?(?:the\s+)?reviews?\s+(?:for|of|on))\s+",
        r"^\s*(?:the|a|an|this|that)\s+",
        r"\s+(?:really\s+)?(?:worth\s+(?:it|buying|getting|the\s+money)|any\s+good|good|"
        r"a\s+good\s+(?:buy|choice|idea|deal))(?:\s+or\s+not)?\s*[?.!]*\s*$",
        r"\s+reviews?\s*[?.!]*\s*$",
        r"\s*[?.!]+\s*$",
    )
]


def clean_query(query: str) -> str:
    """The product in a question: 'Is the Garmin Forerunner worth it?' -> 'Garmin Forerunner'."""
    text = " ".join(query.split())
    changed = True
    while changed:
        before = text
        for part in QUESTION_PARTS:
            text = part.sub("", text)
        changed = text != before
    return text.strip() or " ".join(query.split())


def product_name(query: str) -> str:
    known = catalog.find_product(query)
    return known.name if known else clean_query(query)


# Category words a review title often leaves out ("Garmin Forerunner 265 review").
GENERIC_WORDS = {
    "watch", "smartwatch", "headphones", "headphone", "earbuds", "earphones", "phone",
    "smartphone", "laptop", "tablet", "tv", "television", "speaker", "camera", "device",
}  # fmt: skip


def names_product(title: str, product: str) -> bool:
    """Looser than matches_product, for review pages: the product's distinctive words."""
    if matches_product(title, product):
        return True
    wanted = set(re.findall(r"[a-z0-9]+", product.lower())) - GENERIC_WORDS
    found = set(re.findall(r"[a-z0-9]+", title.lower()))
    return bool(wanted) and len(wanted & found) / len(wanted) >= 0.67


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


STORE_SOURCES = {"bestbuy_api", "serpapi_amazon", "serpapi_shopping", "amazon_search_page"}


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


STORE_NAMES = {"amazon.com": "Amazon", "bestbuy.com": "Best Buy"}


def _popularity(offer) -> float:
    """Stars weighted by how many people rated it: 4.5 from 17,000 beats 5.0 from 4."""
    import math

    return (offer.rating or 0) * math.log10((offer.review_count or 0) + 10)


async def browse(req):
    """What a store lists in a category within budget: "smart watches under $50 at Amazon"."""
    from .merchants import WRONG_ITEM
    from .models import Source
    from .stores import amazon_search, amazon_search_url, bestbuy_search

    category = clean_query(req.product)
    budget = req.max_price
    store = req.store
    within = f" under ${budget:g}" if budget is not None else ""
    where = STORE_NAMES.get(store, store) if store else "Amazon and Best Buy"
    search_url = amazon_search_url(category, budget)
    sources = [Source(title=f"Amazon results: {category}{within}", url=search_url)]
    if store not in (None, *STORE_NAMES):
        return PriceCompareResponse(
            product=category, offers=[], data_source="unavailable", notice=None,
            sources=sources,
            summary=f"I can read Amazon and Best Buy listings, not {store}. "
            f"Amazon's results for {category}{within} are linked below.",
        )  # fmt: skip
    report("searching", f"Reading {where} listings for {category}{within}")
    searches = []
    if store in (None, "amazon.com"):
        searches.append(amazon_search(category, budget))
    if store in (None, "bestbuy.com"):
        searches.append(bestbuy_search(category, budget))
    found = [o for group in await asyncio.gather(*searches) for o in group]
    offers = [
        o for o in found
        if not WRONG_ITEM.search(o.product) and (budget is None or o.price < budget)
    ]  # fmt: skip
    offers.sort(key=_popularity, reverse=True)
    offers = offers[:8]
    if not offers:
        return PriceCompareResponse(
            product=category, offers=[], data_source="store_listings", notice=None,
            sources=sources,
            summary=f"I couldn't read {where} listings for {category}{within} right now. "
            "Amazon's filtered results are linked below.",
        )  # fmt: skip
    report("store_listings", f"Found {len(found)} listings; ranking by rating")
    cheapest = min(offers, key=lambda o: o.price)

    def line(o):
        stars = f", {o.rating}★" if o.rating else ""
        count = f" from {o.review_count:,} ratings" if o.review_count else ""
        return f"{o.product[:70]}: ${o.price:.2f}{stars}{count}"

    summary = (
        f"Yes. {where} has {category}{within}. Top rated: "
        + "; ".join(line(o) for o in offers[:3])
        + f". Cheapest: ${cheapest.price:.2f}."
    )
    return PriceCompareResponse(
        product=category, offers=offers, best=offers[0], summary=summary,
        data_source="store_listings", notice=None, sources=sources,
    )  # fmt: skip


async def compare(req):
    from .merchants import best_offer, rank

    if getattr(req, "browse", False):
        return await browse(req)
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
    if not offers or getattr(req, "deep", False):
        # A second, store-focused query widens the pool only when the first found nothing.
        try:
            more, _ = await search(f"buy {name} online store")
        except SearchUnavailable:
            more = []
        seen = {s.url for s in sources}
        more = [s for s in more if s.url not in seen]
        sources += more
        offers += await _candidates(more, name)
    meta["sources"] = sources
    # Stage 2: rank by trust, then price; only clear mismatches were dropped.
    offers = rank(offers)
    listings = await listings_task
    if listings:
        meta["data_source"] = "store_api+" + meta.get("data_source", "web")
    offers = listings + offers
    store = getattr(req, "store", None)
    if store:
        # "at Amazon": only that store's offers count; say so if it had none.
        at_store = [o for o in offers if o.store.endswith(store)]
        if not at_store:
            return PriceCompareResponse(
                product=name, offers=offers, best=None, savings=None, **meta,
                summary=f"No {STORE_NAMES.get(store, store)} listing for {name} was found"
                + (f" under ${budget:g}" if budget is not None else "")
                + ". Other stores' offers are listed.",
            )  # fmt: skip
        offers = at_store
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
    if isinstance(found, SearchUnavailable):
        # The category search only adds source links; the candidates are checked on their own.
        found = ([], {"data_source": "unavailable", "sources": [],
                      "notice": "Category search unavailable; each candidate was checked directly."})
    elif isinstance(found, BaseException):
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
    if not candidates and not priced:
        # Not in the catalog and no store listings: use what the web compares it with.
        found_priced, found_unpriced = await compared_alternatives(name, req.max_price)
        priced += found_priced
        unpriced += found_unpriced
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


# "Garmin Forerunner 55 vs Coros Pace 3: Which is better?" names a real competitor.
VERSUS = re.compile(r"\s+(?:vs\.?|versus)\s+", re.IGNORECASE)
HEADLINE_CLUTTER = re.compile(
    r"\s*(?:\b(?:review|reviews|which|better|best|comparison|compared|winner|should|buy|"
    r"guide|tested|specs|differences?|hands[- ]on|in\s+20\d\d)\b|\b20\d\d\b).*$",
    re.IGNORECASE,
)


def _same_product(a: str, b: str) -> bool:
    from difflib import SequenceMatcher

    # Different model numbers are different products ("Forerunner 165" vs "Forerunner 55").
    da, db = set(re.findall(r"\d+", a)), set(re.findall(r"\d+", b))
    if da and db and da != db:
        return False
    na, nb = re.sub(r"[^a-z0-9]", "", a.lower()), re.sub(r"[^a-z0-9]", "", b.lower())
    return names_product(a, b) or names_product(b, a) or SequenceMatcher(None, na, nb).ratio() >= 0.7


def versus_titles(sources, name: str) -> tuple[str | None, list[tuple[str, int]]]:
    """(the product's real name, [(rival, count)]) from "A vs B" headlines.

    The asked-about product sits on one side of nearly every comparison, so the most
    frequent name resembling the request is the product itself ("garmin 5 forerunning
    watch" -> "Garmin Forerunner 55"); everything else it is compared with is a rival.
    """
    from difflib import SequenceMatcher

    counts: dict[str, list] = {}
    for source in sources:
        title = " ".join(source.title.split())
        if not VERSUS.search(title):
            continue
        for part in VERSUS.split(title):
            part = re.split(r"\s*[:|–—?(,]\s*|\s+-\s+", part)[0]
            part = HEADLINE_CLUTTER.sub("", part).strip(" .-'\"")
            if not 2 <= len(part.split()) <= 6 or not re.search(r"[A-Za-z]", part):
                continue
            key = re.sub(r"[^a-z0-9]", "", part.lower())
            entry = counts.setdefault(key, [part, 0])
            entry[1] += 1
    ranked = sorted(counts.values(), key=lambda e: -e[1])
    wanted = re.sub(r"[^a-z0-9]", "", name.lower())

    def resembles(label: str) -> bool:
        flat = re.sub(r"[^a-z0-9]", "", label.lower())
        # A shared brand alone is not enough: "Garmin Fenix" is not the "Garmin Forerunner".
        return _same_product(label, name) or SequenceMatcher(None, flat, wanted).ratio() >= 0.5

    canonical = next((label for label, count in ranked if count >= 2 and resembles(label)), None)
    rivals = [
        (label, count) for label, count in ranked
        if not _same_product(label, name) and not (canonical and _same_product(label, canonical))
    ]
    return canonical, rivals


def compared_products(sources, name: str) -> list[tuple[str, int]]:
    """Products the web compares `name` against, most compared first: [(name, count)]."""
    return versus_titles(sources, name)[1]


async def resolve_product(name: str) -> str:
    """The real product name behind a loose or misspelled one, from comparison headlines."""
    if catalog.find_product(name):
        return name
    try:
        sources, _ = await search(f"{name} vs")
    except SearchUnavailable:
        return name
    return versus_titles(sources, name)[0] or name


async def compared_alternatives(name: str, max_price: float | None):
    """(priced, unpriced) alternatives from products commonly compared with `name`."""
    try:
        sources, _ = await search(f"{name} vs")
    except SearchUnavailable:
        return [], []
    rivals = compared_products(sources, name)[:3]
    if not rivals:
        return [], []
    report("comparing", f"Checking prices for {', '.join(r for r, _ in rivals)}")
    results = await asyncio.gather(
        *(compare(PriceCompareRequest(product=r, max_price=max_price)) for r, _ in rivals),
        return_exceptions=True,
    )
    priced, unpriced = [], []
    for (rival, count), result in zip(rivals, results):
        why = f"Often compared with {name}" + (f" ({count} comparisons)" if count > 1 else "")
        best = None if isinstance(result, BaseException) else result.best
        if best is None:
            unpriced.append(Alternative(product=rival, price=None, url=_research_url(rival),
                                        reason=f"{why}; no store price could be read, check it yourself"))
        elif max_price is None or best.price < max_price:
            priced.append(Alternative(product=rival, price=best.price, rating=best.rating, url=best.url,
                                      store=best.store, cart_url=best.cart_url,
                                      reason=f"{why}; {best.trust}-trust price from {best.store}"))
    return priced, unpriced


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


VERDICT_LIMIT = 700
VERDICT_PROMPT = (
    "You advise a shopper using expert reviews. You are given excerpts from several "
    "reviews of one product. Write 3 to 5 plain sentences, at most 700 characters: the "
    "overall verdict, who it is best for, and the main reasons to hesitate. Use only what "
    "the reviews say; do not add facts or numbers they do not state. If reviewers "
    "disagree, say so. No markdown or lists."
)


def combine_verdict(per_source: list[tuple[str, list[str]]]) -> str:
    """Round-robin across sources so no single site dominates; whole sentences only."""
    out, used = "", set()
    for round_ in range(3):
        for _, picked in per_source:
            if round_ >= len(picked):
                continue
            s = picked[round_]
            key = frozenset(s.lower().split())
            if any(len(key & u) / max(len(key | u), 1) >= 0.5 for u in used):
                continue
            candidate = f"{out} {s}".strip()
            if len(candidate) > VERDICT_LIMIT:
                return out
            out, used = candidate, used | {key}
    return out


async def reviews(req):
    from .expert_reviews import drop_contradictions, parse_review, site_name, text_review
    from .merchants import MALFORMED, fetch_page

    name = product_name(req.product)
    sources, meta = await search(f"{name} review")
    leads = [s for s in sources if names_product(f"{s.title} {s.snippet}", name)]
    urls = list(dict.fromkeys(s.url for s in leads or sources))[:6]
    report("reading", f"Reading {len(urls)} review pages")
    pages = await asyncio.gather(*(fetch_page(url) for url in urls))
    ratings, pros, cons, per_source, read_from = [], [], [], [], []
    for url, page in zip(urls, pages):
        if not page:
            continue
        try:
            rating, good, bad = parse_review(page, url, name)
            # No structured review: read the page's own score, pros/cons and verdict.
            text_rating, text_good, text_bad, verdict = text_review(page, url, name)
        except MALFORMED:
            continue
        rating = rating or text_rating
        good, bad = good or text_good, bad or text_bad
        if not (rating or good or bad or verdict):
            continue
        source = site_name(url)
        read_from.append(source)
        # One score per site: a review page and a product page can both carry it.
        if rating and rating.source not in {r.source for r in ratings}:
            ratings.append(rating)
        pros = merge_notes(pros, good)
        cons = merge_notes(cons, bad)
        if verdict:
            per_source.append((source, verdict))
    report("summarizing", f"Writing advice from {len(read_from)} reviews")
    average = round(sum(r.rating for r in ratings) / len(ratings), 1) if ratings else None
    verdict = await llm_verdict(name, per_source, pros, cons) or combine_verdict(per_source)
    if ratings:
        scores = ", ".join(f"{r.source} {r.rating:g}" for r in ratings)
        summary = f"Reviewers rate {name} {average}/5 on average ({scores})."
    elif read_from:
        summary = f"Advice from {len(read_from)} reviews of {name} ({', '.join(dict.fromkeys(read_from))})."
    else:
        names = ", ".join(dict.fromkeys(site_name(s.url) for s in sources[:5]))
        summary = (
            f"Found review sources for {name} ({names}), but their pages couldn't be read. "
            "Open them to read the reviews."
        )
    if verdict:
        summary += f" {verdict}"
    else:
        if pros:
            summary += f" Pros: {pros[0]}."
        if cons:
            summary += f" Cons: {cons[0]}."
    best = max(ratings, key=lambda r: r.rating, default=None)
    return ReviewsResponse(
        product=name,
        rating=average,
        review_count=len(ratings),
        ratings=ratings,
        pros=pros[:5],
        cons=drop_contradictions(pros, cons)[:5],
        review_url=best.url if best else (urls[0] if urls else sources[0].url),
        summary=summary,
        verdict=verdict,
        read_from=list(dict.fromkeys(read_from))[:6],
        **meta,
    )


async def llm_verdict(name: str, per_source, pros: list[str], cons: list[str]) -> str:
    from . import llm
    from .news import numbers_supported

    if not llm.has_key() or not (per_source or pros or cons):
        return ""
    text = "\n".join(f"[{source}] {' '.join(sentences)}" for source, sentences in per_source)
    if pros:
        text += "\nPros mentioned: " + "; ".join(pros)
    if cons:
        text += "\nCons mentioned: " + "; ".join(cons)
    answer = await llm.complete(VERDICT_PROMPT, f"Product: {name}\n{text}")
    if not answer:
        return ""
    answer = " ".join(re.sub(r"[*_#`>]+", "", answer).split())[:VERDICT_LIMIT]
    # Never pass on a number the reviews do not contain.
    return answer if len(answer) > 60 and numbers_supported(answer, text) else ""