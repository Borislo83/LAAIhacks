"""Mock product catalog.

Every agent reads product data through the functions in this module, so swapping
in a real price/review API only means changing this file.
"""

import re
from dataclasses import dataclass, field
from functools import lru_cache


@dataclass(frozen=True)
class Product:
    name: str
    category: str
    rating: float
    review_count: int
    offers: dict[str, float]
    pros: list[str] = field(default_factory=list)
    cons: list[str] = field(default_factory=list)
    aliases: tuple[str, ...] = ()


STORE_URLS = {
    "Amazon": "https://www.amazon.com/s?k={q}",
    "Best Buy": "https://www.bestbuy.com/site/searchpage.jsp?st={q}",
    "Walmart": "https://www.walmart.com/search?q={q}",
    "Target": "https://www.target.com/s?searchTerm={q}",
}

PRODUCTS = [
    Product(
        name="Sony WH-1000XM5",
        category="headphones",
        rating=4.6,
        review_count=12840,
        offers={"Amazon": 328.0, "Best Buy": 349.99, "Walmart": 339.0, "Target": 349.99},
        pros=["Best-in-class noise cancelling", "30 hour battery", "Light and comfortable"],
        cons=["Does not fold flat", "Pricey"],
        aliases=("xm5", "wh1000xm5", "sony xm5"),
    ),
    Product(
        name="Bose QuietComfort Ultra Headphones",
        category="headphones",
        rating=4.5,
        review_count=6210,
        offers={"Amazon": 379.0, "Best Buy": 399.0, "Target": 429.0},
        pros=["Excellent comfort", "Strong noise cancelling", "Immersive audio mode"],
        cons=["24 hour battery", "Most expensive in class"],
        aliases=("qc ultra", "bose qc ultra", "quietcomfort ultra"),
    ),
    Product(
        name="Anker Soundcore Space Q45",
        category="headphones",
        rating=4.3,
        review_count=8930,
        offers={"Amazon": 99.99, "Walmart": 109.0},
        pros=["Very good noise cancelling for the price", "50 hour battery"],
        cons=["Bulky case", "Plastic build"],
        aliases=("space q45", "soundcore q45"),
    ),
    Product(
        name="Apple AirPods Pro 2",
        category="earbuds",
        rating=4.7,
        review_count=45120,
        offers={"Amazon": 189.0, "Best Buy": 199.0, "Walmart": 194.0, "Target": 199.99},
        pros=["Seamless iPhone pairing", "Great transparency mode", "USB-C case"],
        cons=["Limited Android features"],
        aliases=("airpods pro", "airpods pro 2"),
    ),
    Product(
        name="Samsung Galaxy Buds3 Pro",
        category="earbuds",
        rating=4.2,
        review_count=3310,
        offers={"Amazon": 179.0, "Best Buy": 199.99, "Samsung": 249.99},
        pros=["Rich sound", "Good noise cancelling"],
        cons=["Best features need a Samsung phone"],
        aliases=("buds3 pro", "galaxy buds 3 pro"),
    ),
    Product(
        name="Nothing Ear (a)",
        category="earbuds",
        rating=4.3,
        review_count=2780,
        offers={"Amazon": 79.0, "Nothing": 99.0},
        pros=["Great value", "Distinctive design"],
        cons=["Average call quality"],
        aliases=("nothing ear a", "ear (a)"),
    ),
]


def _norm(text: str) -> str:
    return "".join(ch for ch in text.lower() if ch.isalnum() or ch == " ").strip()


@lru_cache(maxsize=1024)
def find_product(query: str) -> Product | None:
    """Best catalog match for a free-text product name, or None."""
    q = _norm(query)
    if not q:
        return None
    for product in PRODUCTS:
        names = (product.name, *product.aliases)
        if any(_norm(n) == q for n in names):
            return product
    # A model number or generation the query names must be the product's own: "AirPods
    # Pro 3" contains the alias "airpods pro" but is not the AirPods Pro 2.
    asked = _model_tokens(_norm(BUDGET.sub(" ", query)))
    for product in PRODUCTS:
        names = (product.name, *product.aliases)
        own = set().union(*(_model_tokens(_norm(n)) for n in names))
        if asked - own:
            continue
        if any(_norm(n) in q or q in _norm(n) for n in names):
            return product
    return None


# Prices and budgets in a request are not model numbers ("under $300", "$249.99").
BUDGET = re.compile(
    r"\$\s*\d[\d,]*(?:\.\d+)?|\b(?:under|below|less\s+than|up\s+to|over|above|max(?:imum)?|"
    r"budget(?:\s+of)?)\s*\$?\s*\d[\d,]*(?:\.\d+)?|\b\d[\d,]*(?:\.\d+)?\s*(?:dollars|usd|bucks)\b",
    re.IGNORECASE,
)


def _model_tokens(text: str) -> set[str]:
    """Words holding a digit, with ordinals as numbers ("2nd" -> "2")."""
    return {
        re.sub(r"^(\d+)(?:st|nd|rd|th)$", r"\1", w)
        for w in text.split()
        if any(c.isdigit() for c in w)
    }


def offer_url(store: str, product: Product) -> str:
    template = STORE_URLS.get(store, "https://www.google.com/search?q={q}")
    return template.format(q=product.name.replace(" ", "+"))


def review_url(product: Product) -> str:
    return f"https://www.rtings.com/search?q={product.name.replace(' ', '+')}"


def alternatives_for(product: Product, max_price: float | None = None) -> list[Product]:
    """Same-category products that are cheaper or better rated, best value first."""
    base_price = min(product.offers.values())
    limit = max_price if max_price is not None else float("inf")
    candidates = [
        p
        for p in PRODUCTS
        if p.category == product.category
        and p.name != product.name
        and min(p.offers.values()) <= limit
        and (min(p.offers.values()) < base_price or p.rating > product.rating)
    ]
    return sorted(candidates, key=lambda p: min(p.offers.values()) / p.rating)
