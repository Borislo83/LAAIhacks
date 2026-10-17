"""Message models the orchestrator uses to talk to the agents."""

from uagents import Model


class Offer(Model):
    product: str
    store: str
    price: float
    url: str


class PriceCompareRequest(Model):
    product: str
    current_store: str | None = None
    current_price: float | None = None


class PriceCompareResponse(Model):
    product: str
    offers: list[Offer]
    best: Offer | None = None
    savings: float | None = None
    summary: str


class AlternativesRequest(Model):
    product: str
    max_price: float | None = None


class Alternative(Model):
    product: str
    price: float
    rating: float
    url: str
    reason: str


class AlternativesResponse(Model):
    product: str
    alternatives: list[Alternative]
    summary: str


class ReviewsRequest(Model):
    product: str


class ReviewsResponse(Model):
    product: str
    rating: float | None = None
    review_count: int = 0
    pros: list[str] = []
    cons: list[str] = []
    review_url: str | None = None
    summary: str
