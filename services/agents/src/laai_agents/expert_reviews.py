"""Expert review evidence from schema.org Review markup on review sites.

Sites like PCMag, What Hi-Fi, TechRadar, SoundGuys and Trusted Reviews publish each
review's rating and pros/cons as JSON-LD. Ratings are normalized to a /5 scale and
only counted when the reviewed item names the product. Pages are read with the same
read-only, private-network-refusing fetcher as merchant pages.
"""

import json

from lxml import html

from .merchants import store_of as site_name
from .models import SourceRating


def _nodes(node):
    if isinstance(node, list):
        for child in node:
            yield from _nodes(child)
    elif isinstance(node, dict):
        yield node
        for value in node.values():
            yield from _nodes(value)


def _notes(value) -> list[str]:
    """positiveNotes/negativeNotes: an ItemList, a list, or plain text."""
    if isinstance(value, dict):
        value = value.get("itemListElement", value.get("name", []))
    if isinstance(value, (str, dict)):
        value = [value]
    notes = []
    for item in value if isinstance(value, list) else []:
        text = item.get("name", "") if isinstance(item, dict) else item
        text = " ".join(str(text).split())
        if text:
            notes.append(text[:120])
    return notes


def to_five(value, best=None) -> float | None:
    try:
        rating = float(value)
        scale = float(best) if best not in (None, "") else None
    except (TypeError, ValueError):
        return None
    if scale is None:
        # No bestRating given: infer the common 5, 10 and 100 point scales.
        scale = 5 if rating <= 5 else 10 if rating <= 10 else 100
    if not 0 < rating <= scale:
        return None
    return round(rating / scale * 5, 1)


def parse_review(page: str, url: str, product: str) -> tuple[SourceRating | None, list, list]:
    """The page's expert rating for `product`, plus its pros and cons."""
    from .live import matches_product

    doc = html.fromstring(page)
    title = " ".join(doc.xpath("//title/text()"))
    for text in doc.xpath('//script[@type="application/ld+json"]/text()'):
        try:
            nodes = list(_nodes(json.loads(text)))
        except (ValueError, TypeError):
            continue
        for node in nodes:
            score = node.get("reviewRating")
            if not isinstance(score, dict):
                continue
            item = node.get("itemReviewed")
            names = [
                str(item.get("name", "")) if isinstance(item, dict) else "",
                str(node.get("name", "")),
                str(node.get("headline", "")),
                title,
            ]
            if not any(matches_product(name, product) for name in names):
                continue
            rating = to_five(score.get("ratingValue"), score.get("bestRating"))
            if rating is None:
                continue
            # Pros/cons sit on the Review or on the Product it reviews.
            holder = item if isinstance(item, dict) else {}
            return (
                SourceRating(source=site_name(url), rating=rating, url=url),
                _notes(node.get("positiveNotes") or holder.get("positiveNotes") or []),
                _notes(node.get("negativeNotes") or holder.get("negativeNotes") or []),
            )
    return None, [], []
