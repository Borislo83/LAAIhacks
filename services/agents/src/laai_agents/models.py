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
    # Store listings (Best Buy API, SerpApi) carry these; web evidence does not.
    cart_url: str | None = None
    rating: float | None = None
    review_count: int | None = None


class PriceCompareRequest(Model):
    product: str
    progress_id: str | None = Field(default=None, min_length=32, max_length=64)
    current_store: str | None = None
    current_price: float | None = None
    max_price: float | None = None
    # Also run the store-focused search, e.g. when the verifier rejected every lead.
    deep: bool = False


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
    store: str | None = None
    # Official add-to-cart link, only for Amazon and Best Buy listings.
    cart_url: str | None = None


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


class Pick(Model):
    """One of the three recommendations: best price, alternative, recommended."""

    label: str
    product: str
    price: float | None = None
    store: str | None = None
    url: str
    cart_url: str | None = None
    rating: float | None = None
    reason: str


class CartRequest(Model):
    product_id: str = Field(min_length=1, max_length=20)  # Amazon ASIN or Best Buy SKU
    quantity: int = Field(default=1, ge=1, le=10)


class CartLink(Model):
    store: str
    product_id: str
    quantity: int
    cart_url: str | None = None
    error: str | None = None
    summary: str


class ConsiderResponse(Model):
    product: str
    max_price: float | None = None
    price: PriceCompareResponse | None = None
    reviews: ReviewsResponse | None = None
    alternatives: AlternativesResponse | None = None
    # Reviews for the top alternative, so the two products can be compared.
    alternative_reviews: ReviewsResponse | None = None
    picks: list[Pick] = Field(default_factory=list)
    # The verifier agent's audit of the price offers, when a verifier is available.
    verification: dict | None = None
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


class NewsRequest(Model):
    query: str = Field(min_length=1, max_length=300)
    max_sources: int = Field(default=8, ge=3, le=12)
    progress_id: str | None = Field(default=None, min_length=32, max_length=64)


class NewsSource(Model):
    title: str
    url: str
    publisher: str
    published_at: str | None = None


class NewsStory(Model):
    """One story in a news brief."""

    section: str
    headline: str
    summary: str
    url: str
    publisher: str
    published_at: str | None = None
    image_url: str | None = None
    # How many outlets in the results covered this story: a rough importance signal.
    coverage: int = 1
    # Newsletter paragraph written from the full text of the articles read for it.
    body: str = ""
    # The outlets whose article text the paragraph was drawn from.
    read_from: list[str] = Field(default_factory=list, max_items=3)


class NewsResponse(Model):
    query: str
    headline: str | None = None
    # 2-4 plain sentences, at most 600 characters, drawn only from the sources.
    summary: str | None = None
    image_url: str | None = None
    image_alt: str | None = None
    image_source_url: str | None = None
    # Most relevant first.
    sources: list[NewsSource] = Field(default_factory=list, max_items=8)
    error: str | None = None
    # A general request ("brief me on the news") returns several stories instead.
    brief: bool = False
    stories: list[NewsStory] = Field(default_factory=list, max_items=10)
