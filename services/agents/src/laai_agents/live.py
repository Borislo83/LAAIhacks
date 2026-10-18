"""Live search evidence: broad price candidates from any store, ranked by trust."""

import asyncio
import re
import time
from datetime import UTC, datetime
from difflib import SequenceMatcher
from functools import lru_cache
from urllib.parse import quote_plus, urlsplit

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
    """Looser than matches_product, for review pages: the product's distinctive words.
    Another generation or model is never this product ("AirPods Pro 2" for "Pro 3")."""
    if matches_product(title, product):
        return True
    if _other_model(title, product) or _model_inside(title, product):
        return False
    wanted = set(_tokens(product)) - GENERIC_WORDS
    found = set(_tokens(title))
    said = sum(has_word(w, found) for w in wanted)
    return bool(wanted) and said / len(wanted) >= 0.67


@lru_cache(maxsize=4096)
def _close_spelling(word: str, token: str) -> bool:
    # Same start and mostly the same letters: "forerunning" and "forerunner" (0.76).
    return word[:4] == token[:4] and SequenceMatcher(None, word, token).ratio() >= 0.75


def has_word(word: str, tokens: set[str]) -> bool:
    """The word, or a close spelling of a long one ("forerunning" for "forerunner").
    Words with digits are model numbers and must match exactly: XM5 is not XM6."""
    if word in tokens:
        return True
    if any(c.isdigit() for c in word):
        # Stores glue a color code onto long model numbers: "WH-1000XM5B" is the black
        # XM5. Short ones stay exact, since "8a" is not the "8".
        return len(word) >= 5 and any(
            len(t) == len(word) + 1 and t.startswith(word) and t[-1].isalpha() for t in tokens
        )
    if len(word) < 6:
        return False
    return any(
        len(t) >= 6 and not any(c.isdigit() for c in t) and _close_spelling(word, t)
        for t in tokens
    )


ORDINAL = re.compile(r"^(\d+)(?:st|nd|rd|th)$")
GEN_WORDS = {"gen", "generation"}
SPELLED = {"first": "1", "second": "2", "third": "3", "fourth": "4", "fifth": "5"}


def _tokens(text: str) -> list[str]:
    """Lowercase words, with every way of writing a generation as its number: "2nd
    generation", "(2nd gen)", "Gen 2", "second generation" and "2" are all "2"."""
    words = re.findall(r"[a-z0-9]+", text.lower())
    out = []
    for i, word in enumerate(words):
        nxt = words[i + 1] if i + 1 < len(words) else ""
        if (m := ORDINAL.match(word)) and nxt in GEN_WORDS:
            out.append(m[1])
        elif word in SPELLED and nxt in GEN_WORDS:
            out.append(SPELLED[word])
        elif word in GEN_WORDS:
            continue
        elif m := ORDINAL.match(word):
            out.append(m[1])
        else:
            out.append(word)
    return out


@lru_cache(maxsize=512)
def _name_words(product: str) -> tuple[frozenset[str], ...]:
    """Word sets for the product and its catalog names, ignoring one-word names."""
    known = catalog.find_product(product)
    names = [product, *([known.name, *known.aliases] if known else [])]
    return tuple(
        frozenset(words)
        for name in names
        if len(words := _tokens(name)) >= 2
    )


def _glued(tokens: set[str], words) -> set[str]:
    """Stores write "WH-1000XM5" as "WH1000XM5" or "WH1000XM5P" (pink): a title token
    that is two adjacent name words glued together (plus at most one color letter)
    counts as both words. The glued model must hold a digit, so XM5 never matches XM6."""
    found = set()
    for a, b in zip(words, words[1:]):
        joined = a + b
        if len(joined) < 5 or not any(c.isdigit() for c in joined):
            continue
        if any(t == joined or (len(t) == len(joined) + 1 and t.startswith(joined) and t[-1].isalpha())
               for t in tokens):
            found |= {a, b}
    return found


def matches_product(title: str, product: str) -> bool:
    tokens = set(_tokens(title))
    known = catalog.find_product(product)
    for name in [product, *([known.name, *known.aliases] if known else [])]:
        tokens |= _glued(tokens, re.findall(r"[a-z0-9]+", name.lower()))
    if _model_inside(title, product) or _other_model(title, product):
        return False
    return any(
        words <= tokens or all(has_word(w, tokens) for w in words) for words in _name_words(product)
    )


TIERS = r"pro|max|lite|oled|mini|plus|ultra|se|fe"


def _generations(product: str) -> set[str]:
    """The generation numbers a name states: "AirPods Pro 3" -> {"3"}, "(2nd gen)" -> {"2"}."""
    words = _tokens(product)
    return {b for a, b in zip(words, words[1:]) if a.isalpha() and b.isdigit()}


def _other_model(title: str, product: str) -> bool:
    """Another number after the same name is another generation: "AirPods Pro 3" is not
    the "AirPods Pro 2", even though the catalog alias "AirPods Pro" fits both. A
    generation stated anywhere ("... with MagSafe Case (2nd generation)") counts too."""
    names = [product, *([known.name] if (known := catalog.find_product(product)) else [])]
    low = title.lower()
    wanted = _generations(product)
    stated = {
        m[1] or m[2] or SPELLED[m[3]]
        for m in re.finditer(
            r"\b(\d+)(?:st|nd|rd|th)?\s*gen(?:eration)?\b|\bgen(?:eration)?\s*(\d+)\b"
            r"|\b(first|second|third|fourth|fifth)\s+gen(?:eration)?\b",
            low,
        )
    }
    if len(wanted) == 1 and stated and not stated & wanted:
        return True
    # Another tier right after the name: "AirPods Pro" is not the "AirPods 4", and the
    # "Switch OLED" or "Switch Lite" is not the "Switch 2".
    own = set(_tokens(product))
    for word in own:
        if word.isalpha() and word not in GENERIC_WORDS and any(
            m[1] not in own for m in re.finditer(rf"\b{re.escape(word)}\s+({TIERS})\b", low)
        ):
            return True
    for name in names:
        words = _tokens(name)
        for a, b in zip(words, words[1:]):
            # "Pro 3", or a generation spelled out: "Pro (1st Generation)", "Pro 2nd Gen".
            if a.isalpha() and b.isdigit() and any(
                m[1] != b
                for m in re.finditer(rf"\b{a}\s*\(?\s*(\d+)(?:\b|(?:st|nd|rd|th)\s+gen)", low)
            ):
                return True
    return False


def _model_inside(title: str, product: str) -> bool:
    """A model number inside the name is another product: "Galaxy Buds 3 Pro" is not the
    "Galaxy Buds Pro". A number after the name ("Forerunner 55") still matches."""
    words = re.findall(r"[a-z0-9]+", product.lower())
    low = title.lower()
    for a, b in zip(words, words[1:]):
        if a.isalpha() and b.isalpha() and re.search(rf"\b{a}\s*\d+[a-z]*\s+{b}\b", low):
            return True
    return False


def unconfirmed_model(title: str, product: str) -> bool:
    """The listing names the product line but no generation ("Apple AirPods Pro with
    MagSafe Case" for "AirPods Pro 3"): it may be this model or another one."""
    wanted = _generations(product)
    if not wanted or matches_product(title, product) or _other_model(title, product):
        return False
    if _model_inside(title, product):
        return False
    tokens = set(_tokens(re.sub(r"\$\s?[\d,]+(?:\.\d+)?", " ", title)))
    if any(t.isdigit() and len(t) <= 2 for t in tokens):
        return False  # some other number is stated; don't guess
    base = [w for w in _tokens(product) if w not in wanted]
    return len(base) >= 1 and all(has_word(w, tokens) for w in base)


def other_generation(candidate: str, product: str) -> str | None:
    """"older model" / "newer model" when `candidate` is the same line as `product` in
    another generation ("AirPods Pro 2" for "AirPods Pro 3"), else None."""
    mine, theirs = _generations(product), _generations(candidate)
    if len(mine) != 1 or len(theirs) != 1 or mine == theirs:
        return None
    def line(text):
        return {w for w in _tokens(text) if not w.isdigit()} - GENERIC_WORDS - {"apple", "the"}
    # The same line exactly: "AirPods 5" is another tier, not a newer "AirPods Pro".
    a, b = line(product), line(candidate)
    if not a or a != b:
        return None
    return "older model" if int(next(iter(theirs))) < int(next(iter(mine))) else "newer model"


# Price lookups answer by a deadline with what has arrived; stragglers are dropped and
# the answer says it may be incomplete. Answers are reused for a few minutes.
PRICE_DEADLINE = 3.5
MAX_PAGES = 6
MAX_OFFERS = 8
PER_STORE = 3
CACHE_SECONDS = 600
PARTIAL_CACHE_SECONDS = 60
_PRICE_CACHE: dict[tuple, tuple[float, float, PriceCompareResponse]] = {}


async def _candidates(sources, name, found: list | None = None):
    """Snippet leads at once, then offers read from up to MAX_PAGES store pages as each
    page arrives (into `found`, so a deadline keeps whatever was read in time)."""
    from . import merchants
    from .merchants import snippet_offer

    found = [] if found is None else found
    leads = [s for s in sources if matches_product(f"{s.title} {s.snippet}", name)]
    found.extend(offer for s in leads if (offer := snippet_offer(s, name)))
    # A result naming the line but no generation is a labeled lead; its page isn't read.
    found.extend(
        merchants.unconfirmed(offer)
        for s in sources
        if s not in leads and unconfirmed_model(f"{s.title} {s.snippet}", name)
        and (offer := snippet_offer(s, name))
    )
    urls = [u for u in dict.fromkeys(s.url for s in leads) if not merchants.blocked(u)]
    urls = urls[:MAX_PAGES]
    report("validating", f"Checking offers on {len(urls)} merchant pages and ranking evidence")
    async def read_page(url):
        return url, await merchants.fetch_offers(url, name)

    for page in asyncio.as_completed([read_page(url) for url in urls]):
        url, offers = await page
        if offers:
            # A price read from the page replaces that page's snippet lead.
            read = {url, *(o.url for o in offers)}
            found[:] = [o for o in found if o.source != "search_snippet" or o.url not in read]
            found.extend(offers)
    return found


async def _web_offers(name: str, deep: bool, found: list, state: dict) -> None:
    sources, meta = await search(f"{name} price USD")
    state["sources"], state["meta"] = sources, meta
    await _candidates(sources, name, found)
    if deep:
        # A second, store-focused query widens the pool when asked (verifier retry).
        try:
            more, _ = await search(f"buy {name} online store")
        except SearchUnavailable:
            return
        seen = {s.url for s in state["sources"]}
        more = [s for s in more if s.url not in seen]
        state["sources"] = state["sources"] + more
        await _candidates(more, name, found)


STORE_LABELS = {"amazon.com": "Amazon", "bestbuy.com": "Best Buy", "walmart.com": "Walmart",
                "target.com": "Target"}  # fmt: skip


def where_sold(offer) -> str:
    store = STORE_LABELS.get(offer.store, offer.store)
    return f"{store} ({offer.seller})" if offer.seller else store


def _diverse(offers, limit: int = MAX_OFFERS):
    """At most PER_STORE offers per store and seller, so one listing's colors don't fill
    the list (each marketplace seller counts as its own shop)."""
    picked, count = [], {}
    for offer in offers:
        key = (offer.store, offer.seller)
        if count.get(key, 0) < PER_STORE:
            picked.append(offer)
            count[key] = count.get(key, 0) + 1
    return picked[:limit]


def price_summary(name: str, offers, best, budget=None) -> str:
    if not offers:
        return (
            f"Found sources for {name}, but none quoted a clear USD price. "
            "Open the sources to check current offers."
        )
    verified = sum(o.verified for o in offers)
    head = f"{len(offers)} offer{'s' if len(offers) != 1 else ''}, {verified} verified"
    if best is None:
        low = min(offers, key=lambda o: o.price)
        return (
            f"{head}: only used or refurbished listings, lowest ${low.price:.2f} at "
            f"{where_sold(low)}. Check the condition before buying."
        )
    within = f" under ${budget:.2f}" if budget is not None else ""
    tag = "verified" if best.verified else f"{best.trust} trust, unverified"
    line = f"{head}: best ${best.price:.2f}{within} at {where_sold(best)} ({tag})."
    cheaper = [o for o in offers if o.condition == "new" and o.price < best.price]
    if cheaper:
        low = min(cheaper, key=lambda o: o.price)
        line += f" Lowest unverified lead: ${low.price:.2f} at {where_sold(low)}."
    return line + " Verify the listing before buying."


async def store_listings(name: str, budget: float | None = None):
    """Official store listings for `name` within budget, cheapest first."""
    from .merchants import is_accessory, unconfirmed
    from .stores import store_offers

    offers = await store_offers(name, budget)
    # A model-specific name ("Sony WH-1000XM5") must match; a one-word one can't be checked.
    specific = len(re.findall(r"[a-z0-9]+", name.lower())) >= 2
    keep = [
        o
        for o in offers
        if not is_accessory(o.product, name) and (not specific or matches_product(o.product, name))
    ]
    # No generation stated ("Apple AirPods Pro" for "AirPods Pro 3"): kept, labeled.
    keep += [
        unconfirmed(o)
        for o in offers
        if specific and not is_accessory(o.product, name) and unconfirmed_model(o.product, name)
        # The listing's address can still name the generation ("...-1st-Generation").
        and not _other_model(" ".join(re.split(r"[/_-]+", urlsplit(o.url).path)), name)
    ]
    if keep:
        report("store_listings", f"Found {len(keep)} store listings from official sources")
    return sorted(keep, key=lambda o: o.price)


STORE_NAMES = {"amazon.com": "Amazon", "bestbuy.com": "Best Buy"}


def sold_at(offer_store: str, store: str) -> bool:
    """Offers name their store by domain ("walmart.com") or, from Google Shopping, by
    name ("Walmart", "Best Buy", "Walmart - Seller"); both count for "walmart.com"."""
    seller = offer_store.lower()
    stem = re.sub(r"[^a-z0-9]", "", store.lower().split(".")[0])
    return seller.endswith(store) or re.sub(r"[^a-z0-9]", "", seller).startswith(stem)


def _popularity(offer) -> float:
    """Stars weighted by how many people rated it: 4.5 from 17,000 beats 5.0 from 4."""
    import math

    return (offer.rating or 0) * math.log10((offer.review_count or 0) + 10)


async def browse(req):
    """What a store lists in a category within budget: "smart watches under $50 at Amazon"."""
    from .merchants import is_accessory
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
        if not is_accessory(o.product, category) and (budget is None or o.price < budget)
        and o.condition == "new"
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


async def compare(req, deadline: float | None = None):
    """`deadline` (seconds, default PRICE_DEADLINE) lets a caller with less time left get
    the offers found so far instead of being cancelled with nothing."""
    from .merchants import best_offer, rank

    if getattr(req, "browse", False):
        return await browse(req)
    name = product_name(req.product)
    budget = getattr(req, "max_price", None)
    store = getattr(req, "store", None)
    deep = bool(getattr(req, "deep", False))
    key = (name.lower(), budget, store, deep)
    hit = _PRICE_CACHE.get(key)
    if hit and time.monotonic() - hit[0] < hit[1]:
        report("cached", "Reusing prices checked in the last few minutes")
        return hit[2].copy(deep=True)
    # Store listings and the web search (then its pages) run at the same time; whatever
    # has arrived by the deadline is ranked.
    found: list = []
    state: dict = {"sources": [], "meta": None}
    web = asyncio.create_task(_web_offers(name, deep, found, state))
    listings_task = asyncio.create_task(store_listings(name, budget))
    limit = PRICE_DEADLINE if deadline is None else min(deadline, PRICE_DEADLINE)
    done, pending = await asyncio.wait({web, listings_task}, timeout=limit)
    for task in pending:
        task.cancel()
    await asyncio.gather(*pending, return_exceptions=True)
    partial = bool(pending)
    listings = []
    if listings_task in done:
        if listings_task.exception():
            raise listings_task.exception()
        listings = listings_task.result()
    failed = web.exception() if web in done else None
    if failed and (not isinstance(failed, SearchUnavailable) or not listings):
        raise failed
    meta = state["meta"] or {
        "data_source": "unavailable", "notice": None,
        "fetched_at": datetime.now(UTC).isoformat(),
    }  # fmt: skip
    meta = {**meta, "sources": state["sources"]}
    if listings:
        meta["data_source"] = "store_api+" + (meta["data_source"] if state["meta"] else "web")
    if partial:
        report("deadline", f"Answering after {limit:.3g}s with the offers found so far")
        meta["notice"] = (meta.get("notice") or "") + (
            f" Some stores didn't answer within {limit:.3g} s; results may be incomplete."
        )
        meta["notice"] = meta["notice"].strip()
    # Rank by trust, then price; only clear mismatches were dropped.
    offers = rank(listings + list(found))
    if store:
        # "at Amazon": only that store's offers count; say so if it had none.
        at_store = [o for o in offers if sold_at(o.store, store)]
        if not at_store:
            return PriceCompareResponse(
                product=name, offers=_diverse(offers), best=None, savings=None, partial=partial,
                **meta,
                summary=f"No {STORE_NAMES.get(store, store)} listing for {name} was found"
                + (f" under ${budget:g}" if budget is not None else "")
                + ". Other stores' offers are listed.",
            )  # fmt: skip
        offers = at_store
    best = best_offer(offers)
    offers = _diverse(offers)
    if best is not None and best not in offers:
        offers = [best, *offers[: MAX_OFFERS - 1]]
    result = PriceCompareResponse(
        product=name, offers=offers, best=best, savings=None, partial=partial,
        summary=price_summary(name, offers, best, budget), **meta,
    )  # fmt: skip
    # A lookup cut short for a caller with little time left isn't reused for a full one.
    if (offers or not partial) and not (partial and limit < PRICE_DEADLINE):
        while len(_PRICE_CACHE) >= 256:
            del _PRICE_CACHE[next(iter(_PRICE_CACHE))]
        ttl = PARTIAL_CACHE_SECONDS if partial else CACHE_SECONDS
        _PRICE_CACHE[key] = (time.monotonic(), ttl, result)
    return result.copy(deep=True)


async def store_alternatives(name, known, max_price):
    """Other brands in the same category from store listings, best rated first."""
    from .merchants import is_accessory
    from .stores import store_offers

    words = re.findall(r"[A-Za-z0-9]+", name)
    # A known product uses its catalog category; otherwise only a category word in the
    # name ("Sony headphones" -> "headphones") says what to look for.
    query = known.category if known else next(
        (w.lower() for w in words if w.lower() in GENERIC_WORDS), None
    )
    if not query:
        return []
    brand = (known.name if known else name).split()[0].lower()
    offers = await store_offers(query, max_price)
    picked = [
        o
        for o in offers
        if not is_accessory(o.product, query)
        and o.condition == "new"
        # A store search for "headphones" also lists earbuds and speakers: the listing
        # must name the category itself.
        and re.search(rf"\b{re.escape(query.rstrip('s'))}s?\b", o.product, re.IGNORECASE)
        and brand not in o.product.lower()
        and not matches_product(o.product, name)
    ]
    picked.sort(key=lambda o: (-(o.rating or 0) * (o.review_count or 0) ** 0.1, o.price))
    # One listing per model: colors of the same product share their first words.
    picked = list({_short(o.product, 4).lower(): o for o in reversed(picked)}.values())[::-1]
    return [
        Alternative(
            product=o.product,
            price=o.price,
            rating=o.rating,
            url=o.url,
            store=o.store,
            cart_url=o.cart_url,
            image=o.image,
            verified=o.trust == "high",
            reason=(f"{o.rating}★ on {where_sold(o)}" if o.rating else f"Listed on {where_sold(o)}")
            + (f", under ${max_price:.2f}" if max_price is not None else "")
            + ("" if o.trust == "high" else "; unverified marketplace price"),
        )
        for o in picked[:5]
    ]


def _short(title: str, words: int = 8) -> str:
    """The start of a long store title: "Beats Solo 4 - Wireless On-Ear ..." -> "Beats Solo 4"."""
    head = re.split(r"\s[-–|,(]\s?|,\s", title)[0]
    return " ".join(head.split()[:words])


ALT_DEADLINE = 4.5
MAX_ALTERNATIVES = 8
_ALT_CACHE: dict[tuple, tuple[float, float, AlternativesResponse]] = {}


def _priced(best, product: str, reason: str) -> Alternative:
    tag = "verified" if best.verified else f"{best.trust}-trust, unverified"
    return Alternative(
        product=product, price=best.price, rating=best.rating, url=best.url,
        store=where_sold(best), cart_url=best.cart_url, image=best.image,
        verified=best.verified, reason=f"{reason}; {tag} price from {where_sold(best)}",
    )  # fmt: skip


async def alternatives(req):
    name = product_name(req.product)
    key = (name.lower(), req.max_price)
    hit = _ALT_CACHE.get(key)
    if hit and time.monotonic() - hit[0] < hit[1]:
        report("cached", "Reusing alternatives checked in the last few minutes")
        return hit[2].copy(deep=True)
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
    ][:4]
    # Known names/category are seeds only. Each candidate needs its own live price
    # evidence. The category search, store listings, candidate prices and (for products
    # outside the catalog) the web's own comparisons all run at once, until the deadline.
    jobs = {
        "search": search(f"{name} alternatives {category} {budget} USD"),
        "stores": store_alternatives(name, known, req.max_price),
        **{
            f"candidate:{i}": compare(PriceCompareRequest(product=p.name, max_price=req.max_price))
            for i, p in enumerate(candidates)
        },
    }
    if not candidates:
        until = time.monotonic() + ALT_DEADLINE - 0.2
        jobs["compared"] = compared_alternatives(name, req.max_price, until)
    tasks = {label: asyncio.create_task(job) for label, job in jobs.items()}
    done, pending = await asyncio.wait(tasks.values(), timeout=ALT_DEADLINE)
    for task in pending:
        task.cancel()
    await asyncio.gather(*pending, return_exceptions=True)
    partial = bool(pending)

    def result(label):
        task = tasks.get(label)
        if task is None or task not in done or task.cancelled():
            return None
        error = task.exception()
        if error is not None and not isinstance(error, Exception):
            raise error
        return error or task.result()

    found = result("search")
    if found is None or isinstance(found, SearchUnavailable):
        # The category search only adds source links; the candidates are checked on their own.
        why = "didn't finish in time" if found is None else "unavailable"
        found = ([], {"data_source": "unavailable", "sources": [],
                      "notice": f"Category search {why}; each candidate was checked directly."})
    elif isinstance(found, BaseException):
        raise found
    sources, meta = found
    sources = list(sources)
    report("comparing", f"Comparing {len(candidates)} candidates against the budget")
    stored = result("stores")
    priced = [] if stored is None or isinstance(stored, BaseException) else list(stored)
    unpriced = []
    for i, product in enumerate(candidates):
        outcome = result(f"candidate:{i}")
        best = None if outcome is None or isinstance(outcome, BaseException) else outcome.best
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
        priced.append(_priced(best, product.name, "Same category"))
        sources.extend(outcome.sources)
    compared = result("compared")
    if compared and not isinstance(compared, BaseException):
        priced += compared[0]
        unpriced += compared[1]
    # Catalog leads without a price stay last.
    priced = list({a.product.lower(): a for a in reversed(priced)}.values())
    priced.sort(key=lambda a: (not a.verified, -(a.rating or 0), a.price))
    # The product's own older or newer model is not a rival: never the top pick, listed
    # after the rivals and named as what it is.
    same_line = [a for a in priced + unpriced if other_generation(a.product, name)]
    priced = [a for a in priced if not other_generation(a.product, name)]
    unpriced = [a for a in unpriced if not other_generation(a.product, name)]
    options = (priced + unpriced + same_line)[:MAX_ALTERNATIVES]
    within = f" under ${req.max_price:.2f}" if req.max_price is not None else ""
    late = partial or any("in time" in (a.reason or "") for a in unpriced)
    if priced:
        verified = sum(a.verified for a in priced)
        count = len(priced) + len(unpriced)
        summary = (
            f"{count} alternative{'s' if count != 1 else ''}, {len(priced)} priced "
            f"({verified} verified). "
            f"Top pick{within}: {_short(priced[0].product)} at ${priced[0].price:.2f} "
            f"({'verified' if priced[0].verified else 'unverified'}). "
            "Check availability and final checkout price at the source."
        )
    elif unpriced:
        summary = (
            f"Alternatives to consider: {', '.join(a.product for a in unpriced)}. "
            f"No store price for them could be read{' in time' if late else ''}, "
            "so compare prices yourself"
            + (f" and check each is{within}." if within else ".")
        )
    elif same_line:
        summary = f"No rival products to {name} were found{' in time' if partial else ''}."
    elif sources:
        summary = (
            f"Found alternative research sources{within}, but no specific product could be "
            "checked; review the linked sources before choosing."
        )
    else:
        summary = (
            f"No alternatives to {name} could be found{' in time' if partial else ''}. "
            "Try again in a moment."
        )
    if same_line:
        labels = "; ".join(_same_line_label(a, name) for a in same_line)
        summary += f" Same line, other generation: {labels}."
    notice = meta.get("notice")
    if partial:
        notice = ((notice or "") + f" Some lookups didn't finish within {ALT_DEADLINE:g} s; "
                  "results may be incomplete.").strip()  # fmt: skip
    meta = {**meta, "notice": notice, "sources": list({s.url: s for s in sources}.values())[:20]}
    response = AlternativesResponse(
        product=name,
        alternatives=options,
        summary=summary,
        partial=partial,
        **meta,
    )
    if options or not partial:
        while len(_ALT_CACHE) >= 256:
            del _ALT_CACHE[next(iter(_ALT_CACHE))]
        ttl = PARTIAL_CACHE_SECONDS if partial else CACHE_SECONDS
        _ALT_CACHE[key] = (time.monotonic(), ttl, response)
    return response.copy(deep=True)


# "Garmin Forerunner 55 vs Coros Pace 3: Which is better?" names a real competitor.
VERSUS = re.compile(r"\s+(?:vs\.?|versus)\s+", re.IGNORECASE)
HEADLINE_CLUTTER = re.compile(
    r"\s*(?:\b(?:review|reviews|which|better|best|comparison|compared|winner|should|buy|"
    r"guide|tested|specs|differences?|hands[- ]on|in\s+20\d\d)\b|\b20\d\d\b).*$",
    re.IGNORECASE,
)


# Product names don't read like sentences: "Battle of the Buds", "Best Earbuds for Running"
# and "How to Choose" are headlines, not products.
HEADLINE_WORDS = re.compile(
    r"\b(?:of|the|for|and|with|to|my|your|our|how|why|what|when|top|battle|showdown|"
    r"face[- ]?off|ultimate|budget|cheap|vs)\b",
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


def _same_line_label(a, name: str) -> str:
    """'AirPods Pro 2 (older model, $189.00 at Amazon)' for the product's own other model."""
    price = f", ${a.price:.2f} at {a.store}" if a.price is not None else ""
    price += ", unverified" if a.price is not None and not a.verified else ""
    return f"{a.product} ({other_generation(a.product, name)}{price})"


def _merge_names(ranked: list[list]) -> list[list]:
    """One entry per product: "AirPods 5 earbuds" is the "AirPods 5", and "Apple AirPods
    Pro 1" the "AirPods Pro 1" (a category word or a leading brand adds nothing). Counts
    add up; the most compared spelling is kept."""
    merged: list[list] = []
    for label, count in ranked:
        words = _tokens(label)
        key = set(words) - GENERIC_WORDS
        for entry in merged:
            other = _tokens(entry[0])
            known = set(other) - GENERIC_WORDS
            extra = key ^ known
            if key == known or (
                len(extra) == 1
                and (key < known or known < key)
                and next(iter(extra)) == (words if key > known else other)[0]
            ):
                entry[1] += count
                break
        else:
            merged.append([label, count])
    return sorted(merged, key=lambda e: -e[1])


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
        sides = VERSUS.split(title)
        for i, part in enumerate(sides):
            # The product is the piece touching "vs": "Battle of the Buds: Galaxy Buds2 vs
            # Pixel Buds" names Galaxy Buds2 last on the left and Pixel Buds first on the right.
            pieces = re.split(r"\s*[:|–—?(,]\s*|\s+-\s+", part)
            part = pieces[-1] if i < len(sides) - 1 else pieces[0]
            part = HEADLINE_CLUTTER.sub("", part).strip(" .-'\"")
            if not 2 <= len(part.split()) <= 6 or not re.search(r"[A-Za-z]", part):
                continue
            if HEADLINE_WORDS.search(part):
                continue
            key = re.sub(r"[^a-z0-9]", "", part.lower())
            entry = counts.setdefault(key, [part, 0])
            entry[1] += 1
    ranked = _merge_names(sorted(counts.values(), key=lambda e: -e[1]))
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


async def compared_alternatives(name: str, max_price: float | None, until: float | None = None):
    """(priced, unpriced) alternatives from products commonly compared with `name`.
    Rival prices still pending at `until` (time.monotonic) are listed unpriced."""
    try:
        sources, _ = await search(f"{name} vs")
    except SearchUnavailable:
        return [], []
    rivals = compared_products(sources, name)[:5]
    # Real rivals are priced first (searches run two at a time); the product's own other
    # generations, shown only as such, queue behind them.
    rivals.sort(key=lambda r: other_generation(r[0], name) is not None)
    if not rivals:
        return [], []
    report("comparing", f"Checking prices for {', '.join(r for r, _ in rivals)}")
    wait = None if until is None else max(0.2, until - time.monotonic())
    # Each rival's lookup answers by the time left with what it found (cancelling it at
    # `until` would drop the store prices it already had).
    tasks = [
        asyncio.create_task(
            compare(
                PriceCompareRequest(product=r, max_price=max_price),
                deadline=None if wait is None else max(0.1, wait - 0.15),
            )
        )
        for r, _ in rivals
    ]
    _, pending = await asyncio.wait(tasks, timeout=wait)
    for task in pending:
        task.cancel()
    await asyncio.gather(*pending, return_exceptions=True)
    results = [
        task.exception() or task.result() if not task.cancelled() else TimeoutError()
        for task in tasks
    ]
    priced, unpriced = [], []
    for (rival, count), result in zip(rivals, results):
        why = f"Often compared with {name}" + (f" ({count} comparisons)" if count > 1 else "")
        if generation := other_generation(rival, name):
            # The product's own older or newer model is not a rival; say which it is.
            why = f"{generation.capitalize()} of {name}, not a rival"
        best = None if isinstance(result, BaseException) else result.best
        if best is None:
            late = isinstance(result, TimeoutError) or getattr(result, "partial", False)
            unpriced.append(Alternative(
                product=rival, price=None, url=_research_url(rival),
                reason=f"{why}; no store price could be read{' in time' if late else ''}, "
                "check it yourself",
            ))  # fmt: skip
        elif max_price is None or best.price < max_price:
            priced.append(_priced(best, rival, why))
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
    from .expert_reviews import (
        drop_contradictions,
        parse_review,
        site_name,
        text_review,
    )
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