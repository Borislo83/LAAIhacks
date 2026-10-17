"""Message models the orchestrator uses to talk to the agents."""

from pydantic.v1 import Field
from uagents import Model


class Source(Model):
    title: str
    url: str
    snippet: str = ""


class EvidenceResponse(Model):
    data_source: str = "demo_catalog"
    fetched_at: str | None = None
    sources: list[Source] = Field(default_factory=list)
    notice: str | None = "Illustrative demo data; not live shopping claims."
    error: str | None = None


class Offer(Model):
    product: str
    store: str
    price: float
    url: str
    # structured (JSON-LD), page_meta (price tags) or search_snippet; demo data is curated.
    source: str = "structured"
    # high: trusted store + page evidence; medium: one of the two; low: neither.
    trust: str = "high"


class PriceCompareRequest(Model):
    product: str
    progress_id: str | None = Field(default=None, min_length=32, max_length=64)
    current_store: str | None = None
    current_price: float | None = None


class PriceCompareResponse(EvidenceResponse):
    product: str
    offers: list[Offer]
    best: Offer | None = None
    savings: float | None = None
    summary: str


class AlternativesRequest(Model):
    product: str
    progress_id: str | None = Field(default=None, min_length=32, max_length=64)
    max_price: float | None = None


class Alternative(Model):
    product: str
    price: float
    rating: float | None = None
    url: str
    reason: str


class AlternativesResponse(EvidenceResponse):
    product: str
    alternatives: list[Alternative]
    summary: str


class ReviewsRequest(Model):
    product: str
    progress_id: str | None = Field(default=None, min_length=32, max_length=64)


class ReviewsResponse(EvidenceResponse):
    product: str
    rating: float | None = None
    review_count: int = 0
    pros: list[str] = Field(default_factory=list)
    cons: list[str] = Field(default_factory=list)
    review_url: str | None = None
    summary: str


class ConsiderRequest(Model):
    product: str = Field(min_length=1, max_length=500)
    progress_id: str | None = Field(default=None, min_length=32, max_length=64)
    max_price: float | None = Field(default=None, ge=0, allow_inf_nan=False)


class ConsiderResponse(Model):
    product: str
    max_price: float | None = None
    price: PriceCompareResponse | None = None
    reviews: ReviewsResponse | None = None
    alternatives: AlternativesResponse | None = None
    errors: dict[str, str] = Field(default_factory=dict)
    timings_ms: dict[str, int] = Field(default_factory=dict)
    summary: str
