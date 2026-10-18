"""Message models the orchestrator uses to talk to the agents."""

from typing import Literal

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
    # None when no store price could be read; the product is still a valid lead.
    price: float | None = None
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


class SourceRating(Model):
    source: str
    rating: float  # normalized to a 5-point scale
    url: str


class ReviewsResponse(EvidenceResponse):
    product: str
    # Live mode: average of expert ratings on a 5-point scale; review_count counts them.
    rating: float | None = None
    review_count: int = 0
    ratings: list[SourceRating] = Field(default_factory=list)
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
    # Reviews for the top alternative, so the two products can be compared.
    alternative_reviews: ReviewsResponse | None = None
    errors: dict[str, str] = Field(default_factory=dict)
    timings_ms: dict[str, int] = Field(default_factory=dict)
    summary: str


class YouTubeRequest(Model):
    query: str = Field(min_length=1, max_length=300)
    kind: Literal["song", "video", "auto"] = "auto"
    progress_id: str | None = Field(default=None, min_length=32, max_length=64)


class YouTubeVideo(Model):
    title: str
    url: str
    channel: str | None = None


class YouTubeResponse(Model):
    query: str
    kind: Literal["song", "video", "auto"]
    title: str | None = None
    url: str | None = None
    channel: str | None = None
    duration: str | None = None
    alternatives: list[YouTubeVideo] = Field(default_factory=list, max_items=3)
    summary: str
    error: str | None = None
    data_source: str | None = None
