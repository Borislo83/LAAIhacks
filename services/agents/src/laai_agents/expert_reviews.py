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


# --- Reviews without structured data -------------------------------------------------
# Many review sites publish no schema.org Review. Their text still has a score, pros and
# cons lists and a verdict, which is what the user actually wants from a review.
import re  # noqa: E402

PROS_LABEL = re.compile(
    r"^\s*(?:pros|the\s+good|what\s+we\s+(?:like|liked|love)|likes|positives|strengths|"
    r"reasons\s+to\s+buy|for)\s*:?\s*$",
    re.IGNORECASE,
)
CONS_LABEL = re.compile(
    r"^\s*(?:cons|the\s+bad|what\s+we\s+(?:don'?t|didn'?t)\s+like|dislikes|negatives|"
    r"weaknesses|reasons\s+to\s+avoid|against)\s*:?\s*$",
    re.IGNORECASE,
)
VERDICT_HEADING = re.compile(
    r"\b(?:verdict|bottom\s+line|should\s+you\s+buy|conclusion|final\s+thoughts|"
    r"who\s+(?:is\s+it|should\s+buy)|is\s+it\s+worth)",
    re.IGNORECASE,
)
ADVICE = re.compile(
    r"\b(?:worth|recommend|should\s+(?:you\s+)?buy|best\s+for|ideal\s+for|great\s+(?:choice|option|for)|"
    r"if\s+you|downside|drawback|however|but|overall|excellent|impressive|disappoint|lacks?|"
    r"struggles?|value|price|battery|accurate|comfortable)\b",
    re.IGNORECASE,
)
TEXT_SCORE = re.compile(
    r"\b(?:rating|score|scored|verdict|we\s+(?:give|rate)\s+it|overall)\b[^.\d]{0,40}?"
    r"(\d{1,3}(?:\.\d)?)\s*(?:/|out\s+of)\s*(5|10|100)\b",
    re.IGNORECASE,
)
STRONG_ADVICE = re.compile(
    r"\b(?:worth|recommend(?:ed)?|best\s+for|ideal\s+for|should\s+(?:you\s+)?(?:buy|get|skip)|"
    r"(?:great|good|solid)\s+(?:choice|option|value|buy)|value|skip|steer\s+clear|"
    r"overall|downside|drawback|disappoint\w*)\b",
    re.IGNORECASE,
)
PRONOUN = re.compile(r"\b(?:it|its|it's|this|these|they|them)\b", re.IGNORECASE)


def about_product(sentence: str, product: str) -> bool:
    """Names a distinctive word of the product, or refers to it ("it", "this", "these")."""
    from .live import GENERIC_WORDS

    distinctive = set(re.findall(r"[a-z0-9]+", product.lower())) - GENERIC_WORDS
    said = set(re.findall(r"[a-z0-9]+", sentence.lower()))
    return bool(distinctive & said) or bool(PRONOUN.search(sentence))


def drop_contradictions(pros: list[str], cons: list[str]) -> list[str]:
    """A con that only negates a pro ("Not waterproof" vs "Waterproof design") came from
    a page about another model; keep the cons that don't contradict."""
    kept = []
    for con in cons:
        negated = re.match(r"^\s*(?:not|no|non-?)\s*(.+)$", con, re.IGNORECASE)
        if negated:
            core = set(re.findall(r"[a-z]+", negated[1].lower()))
            if any(core and core <= set(re.findall(r"[a-z]+", pro.lower())) for pro in pros):
                continue
        kept.append(con)
    return kept


LABEL_TAGS = ("h2", "h3", "h4", "h5", "strong", "b", "p", "dt", "span", "div", "li")
SKIP_TAGS = ("script", "style", "noscript", "nav", "header", "footer", "aside", "form", "svg")


def _list_after(label) -> list[str]:
    """Items of the first list after a "Pros"/"Cons" label (sibling or nearby)."""
    node, hops = label, 0
    while node is not None and hops < 4:
        nxt = node.getnext()
        while nxt is not None and hops < 4:
            lists = [nxt] if nxt.tag in ("ul", "ol") else nxt.xpath(".//ul|.//ol")
            if lists:
                items = [" ".join(li.text_content().split()) for li in lists[0].xpath("./li")]
                return [i[:120] for i in items if 3 <= len(i) <= 200][:5]
            nxt, hops = nxt.getnext(), hops + 1
        node, hops = node.getparent(), hops + 1
    return []


def text_review(page: str, url: str, product: str) -> tuple[SourceRating | None, list, list, list]:
    """(written score, pros, cons, verdict sentences) from a review page's text."""
    from .live import names_product
    from .news import sentences

    doc = html.fromstring(page)
    for tag in doc.xpath("|".join(f"//{t}" for t in SKIP_TAGS)):
        tag.drop_tree()
    title = " ".join(doc.xpath("//title/text()") + doc.xpath("//h1//text()"))
    if not names_product(title, product):
        return None, [], [], []
    pros, cons = [], []
    for element in doc.iter(*LABEL_TAGS):
        label = " ".join(element.text_content().split())
        if len(label) > 40:
            continue
        if not pros and PROS_LABEL.match(label):
            pros = _list_after(element)
        elif not cons and CONS_LABEL.match(label):
            cons = _list_after(element)
    text = " ".join(" ".join(doc.text_content().split()).split())
    score = None
    found = TEXT_SCORE.search(text)
    if found:
        value = to_five(found[1], found[2])
        if value is not None:
            score = SourceRating(source=site_name(url), rating=value, url=url)
    # Verdict sentences: under a verdict heading first, then advice-like sentences anywhere.
    under_heading, anywhere = [], []
    for heading in doc.iter("h2", "h3", "h4"):
        if VERDICT_HEADING.search(heading.text_content()):
            node = heading.getnext()
            while node is not None and node.tag not in ("h2", "h3", "h4"):
                if node.tag == "p":
                    under_heading.append(" ".join(node.text_content().split()))
                node = node.getnext()
    for p in doc.iter("p"):
        anywhere.append(" ".join(p.text_content().split()))
    picked: list[str] = []
    for block, needed in ((under_heading, 0), (anywhere, 2)):
        # A paragraph that doesn't end like a sentence ("Our rating: 4.5 out of 5") would
        # glue onto the next one.
        block = [b for b in block if b and b[-1] in ".!?\"”"]
        for s in sentences(" ".join(block)):
            if not 8 <= len(s.split()) <= 45 or s[-1] not in ".!?\"”":
                continue
            seen = set(s.lower().split())
            if any(len(seen & set(p.lower().split())) / len(seen | set(p.lower().split())) >= 0.6
                   for p in picked):  # fmt: skip
                continue
            if re.match(r"^\s*(?:note|editor'?s\s+note|disclosure|update)\b", s, re.IGNORECASE):
                continue
            # Outside a verdict section, it must be about this product and give advice.
            if needed and not (about_product(s, product) and STRONG_ADVICE.search(s)):
                continue
            if len(ADVICE.findall(s)) >= max(needed, 1):
                picked.append(s)
            if len(picked) >= 3:
                break
        if len(picked) >= 3:
            break
    return score, pros, cons, picked
