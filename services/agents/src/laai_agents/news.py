"""News summary: a short, sourced overview of recent coverage for a topic.

Search is keyless DDGS news first, then the shared web search. The top results are read
concurrently (HTTPS only, public hosts only, 8 s and 1.5 MB per page, redirects
re-checked), ranked by how well their title and lead match the query plus recency, and
near-duplicate syndicated copies are dropped. The summary comes from ASI:One when a key
is set, given only the extracted text of the top articles; otherwise it is built from the
articles' own lead sentences. A summary with a number the sources do not contain is
discarded for the extractive one. Nothing is guessed when nothing is found.
"""

import asyncio
import ipaddress
import re
from email.utils import parsedate_to_datetime
from html import unescape
from datetime import UTC, datetime, timedelta
from urllib.parse import urljoin, urlsplit, urlunsplit

import aiohttp
from lxml import html as lxml_html

from . import llm, net
from .merchants import fetchable, public_host
from .models import NewsRequest, NewsResponse, NewsSource, NewsStory
from .progress import report, tracked
from .search import SearchUnavailable, search

PAGE_LIMIT = 1_500_000
PAGE_TIMEOUT = aiohttp.ClientTimeout(total=8)
MAX_REDIRECTS = 3
MAX_SOURCES_OUT = 8
LLM_ARTICLES = 5
LLM_CHARS_PER_ARTICLE = 2500
SUMMARY_LIMIT = 600
HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/130.0 Safari/537.36",
    "Accept": "text/html,application/xhtml+xml",
    "Accept-Language": "en-US,en;q=0.9",
}
STOPWORDS = {
    "a", "an", "and", "are", "as", "at", "be", "by", "for", "from", "has", "have", "in",
    "is", "it", "its", "of", "on", "or", "that", "the", "this", "to", "was", "were",
    "what", "whats", "with", "about", "new", "news", "latest", "update", "updates",
}  # fmt: skip

# Request phrasing around the topic, stripped repeatedly from either end.
LEADING = [
    re.compile(p, re.IGNORECASE)
    for p in (
        r"^(?:hey|ok|okay|please|pls|so)\b[\s,]*",
        r"^(?:can|could|would|will)\s+you\s+",
        r"^(?:tell|give|show|get|find|fetch|bring)\s+(?:me\s+)?",
        r"^(?:what's|whats|what\s+is|what\s+are|what's\s+been|any|is\s+there\s+any)\s+",
        r"^(?:going\s+on|happening|new)\s+(?:with|in|on|at)\s+",
        r"^(?:a\s+|the\s+)?(?:quick\s+|short\s+|brief\s+)?(?:summary|rundown|recap|overview)\s+(?:of|on)\s+",
        r"^(?:the\s+)?(?:latest|recent|current|breaking|top|today's)\s+",
        r"^(?:news|headlines|updates?|developments|stories|coverage)\s+",
        r"^(?:on|about|regarding|re|for|with|in|of)\b[\s:]+",
        r"^(?:the\s+)?news\b[\s:]*",
    )
]
TRAILING = [
    re.compile(p, re.IGNORECASE)
    for p in (
        r"[\s?.!]+$",
        r"\s+(?:right\s+now|today|this\s+week|lately|recently|please)$",
        r"\s+(?:in\s+the\s+)?(?:news|headlines|updates?)$",
        r"\s+(?:latest|lately)$",
    )
]


def parse_query(text: str) -> str:
    """ "what's the latest on the Mars sample return mission?" -> "Mars sample return mission"."""
    query = " ".join(text.split())
    changed = True
    while changed:
        before = query
        for pattern in LEADING + TRAILING:
            query = pattern.sub("", query)
        query = query.strip(" \"'“”‘’:,-")
        changed = query != before
    # A request that is all phrasing ("what's the news?") keeps its words rather than nothing.
    return query or " ".join(text.split())


def words(text: str) -> set[str]:
    return {
        w
        for w in re.findall(r"[a-z0-9]+", text.lower().replace("'", ""))
        if w not in STOPWORDS and len(w) > 1
    }


def normalize_date(value) -> str | None:
    """ISO 8601 in UTC, or None when the value is not a date we can read."""
    if not value:
        return None
    text = str(value).strip()
    when = None
    try:
        when = datetime.fromisoformat(text)
    except ValueError:
        try:
            when = parsedate_to_datetime(text)
        except (TypeError, ValueError, IndexError):
            return None
    if when.tzinfo is None:
        when = when.replace(tzinfo=UTC)
    return when.astimezone(UTC).isoformat()


def publisher_of(url: str) -> str:
    return (urlsplit(url).hostname or "").removeprefix("www.")


def canonical(url: str) -> str:
    """Same article behind different tracking parameters or fragments."""
    parts = urlsplit(url)
    return urlunsplit(
        (
            "https",
            (parts.hostname or "").removeprefix("www."),
            parts.path.rstrip("/"),
            "",
            "",
        )
    )


# --- Search -----------------------------------------------------------------------------


def _hit(
    url, title, lead="", publisher=None, published=None, image=None
) -> dict | None:
    url = str(url or "").strip()
    if not fetchable(url):
        return None
    return {
        "url": url,
        "title": " ".join(str(title or "").split())[:200],
        "lead": " ".join(unescape(re.sub(r"<[^>]+>", " ", str(lead or ""))).split())[:600],
        "publisher": " ".join(str(publisher).split())[:80] if publisher else None,
        "published_at": normalize_date(published),
    }


async def ddgs_news(query: str, timelimit: str = "w") -> list[dict]:
    def run():
        from ddgs import DDGS

        return DDGS(timeout=8).news(
            query, region="us-en", safesearch="moderate", timelimit=timelimit, max_results=20
        )

    try:
        rows = await asyncio.wait_for(asyncio.to_thread(run), timeout=20)
    except Exception as exc:
        raise SearchUnavailable("Free news search unavailable") from exc
    return [
        hit
        for row in rows or []
        if (
            hit := _hit(
                row.get("url"),
                row.get("title"),
                row.get("body"),
                row.get("source"),
                row.get("date"),
            )
        )
    ]


async def web_news(query: str) -> list[dict]:
    sources, _ = await search(f"{query} news")
    return [hit for s in sources if (hit := _hit(s.url, s.title, s.snippet))]


PROVIDERS = (("ddgs_news", ddgs_news), ("web_search", web_news))


async def find_hits(query: str) -> list[dict] | None:
    """Hits from the first provider that answers with results; None if none answered."""
    answered = False
    for _, run in PROVIDERS:
        try:
            found = await run(query)
        except (SearchUnavailable, aiohttp.ClientError, TimeoutError, ValueError):
            continue
        answered = True
        if found:
            return found
    return [] if answered else None


# --- Reading pages ----------------------------------------------------------------------


async def fetch_page(url: str) -> str | None:
    """One public HTTPS HTML page, at most PAGE_LIMIT bytes; None if not readable."""
    try:
        for _ in range(MAX_REDIRECTS + 1):
            if not fetchable(url) or not await public_host(urlsplit(url).hostname):
                return None
            async with net.session().get(
                url, allow_redirects=False, headers=HEADERS, timeout=PAGE_TIMEOUT
            ) as response:
                if response.status in {301, 302, 303, 307, 308}:
                    url = urljoin(url, response.headers.get("Location", ""))
                    continue
                if response.status != 200 or "html" not in response.headers.get(
                    "Content-Type", ""
                ):
                    return None
                chunks, size = [], 0
                async for chunk in response.content.iter_chunked(65536):
                    chunks.append(chunk[: PAGE_LIMIT - size])
                    size += len(chunks[-1])
                    # The metadata sits in <head>; a page past the cap is read only that far.
                    if size >= PAGE_LIMIT:
                        break
                page = b"".join(chunks).decode("utf-8", errors="replace")
                return page if page.strip() else None
        return None
    except (TimeoutError, aiohttp.ClientError, ValueError):
        return None


DROP_TAGS = ("script", "style", "noscript", "nav", "header", "footer", "aside", "form",
             "figure", "figcaption", "button", "svg", "iframe")  # fmt: skip


def _meta(doc, *keys: str) -> str | None:
    for key in keys:
        for attr in ("property", "name", "itemprop"):
            found = doc.xpath(f'//meta[@{attr}="{key}"]/@content')
            if found and found[0].strip():
                return " ".join(found[0].split())
    return None


def _json_ld_date(doc) -> str | None:
    for block in doc.xpath('//script[@type="application/ld+json"]/text()'):
        match = re.search(r'"datePublished"\s*:\s*"([^"]+)"', block)
        if match:
            return match[1]
    return None


def _int(value) -> int | None:
    try:
        return int(str(value).strip().removesuffix("px"))
    except (TypeError, ValueError):
        return None


def extract(page: str, url: str) -> dict:
    """Title, image, lead, publisher, published time and main text from one page."""
    doc = lxml_html.fromstring(page)
    title = _meta(doc, "og:title", "twitter:title")
    if not title:
        found = doc.xpath("//title/text()")
        title = " ".join(found[0].split()) if found else None
    image = _meta(doc, "og:image:secure_url", "og:image", "twitter:image")
    published = (
        _meta(
            doc,
            "article:published_time",
            "datePublished",
            "pubdate",
            "date",
            "parsely-pub-date",
            "sailthru.date",
        )
        or _json_ld_date(doc)
        or next(iter(doc.xpath("//time/@datetime")), None)
    )
    info = {
        "title": title,
        "image": urljoin(url, image) if image else None,
        "image_width": _int(_meta(doc, "og:image:width")),
        "image_height": _int(_meta(doc, "og:image:height")),
        "image_alt": _meta(doc, "og:image:alt", "twitter:image:alt"),
        "description": _meta(
            doc, "og:description", "description", "twitter:description"
        ),
        "publisher": _meta(doc, "og:site_name", "application-name", "publisher"),
        "published_at": normalize_date(published),
    }
    for tag in doc.xpath("|".join(f"//{t}" for t in DROP_TAGS)):
        tag.drop_tree()
    roots = doc.xpath("//article") or doc.xpath("//main") or [doc]
    root = max(roots, key=lambda r: len(r.text_content()))
    paragraphs = [" ".join(p.text_content().split()) for p in root.iter("p")]
    info["text"] = "\n".join(p for p in paragraphs if len(p) >= 40)
    return info


def generic_text(text: str | None, url: str, publisher: str | None, fallback: str | None) -> bool:
    """A title or description that names only the site, or a stub shorter than the
    search result's own text, says nothing about the story."""
    if not text:
        return True
    flat = re.sub(r"[^a-z0-9]", "", text.lower())
    site = re.sub(r"[^a-z0-9]", "", publisher_of(url).lower().split(".")[0])
    names = {site, re.sub(r"[^a-z0-9]", "", (publisher or "").lower())} - {""}
    if flat in names or any(flat == n + "news" or flat == n + "com" for n in names):
        return True
    return len(text.split()) <= 2 and len((fallback or "").split()) >= 4


async def read_article(hit: dict) -> dict:
    """The search hit, filled in from its page when the page can be read."""
    article = {**hit, "text": "", "image": None, "image_alt": None,
               "image_width": None, "image_height": None}  # fmt: skip
    page = await fetch_page(hit["url"])
    if not page:
        return article
    try:
        info = extract(page, hit["url"])
    except (ValueError, TypeError):  # malformed markup
        return article
    publisher = info["publisher"] or hit["publisher"]
    title = info["title"]
    if title and generic_text(title, hit["url"], publisher, hit["title"]):
        title = None  # e.g. "MSN": the page is a script shell, the search title is real
    article["title"] = title or hit["title"]
    lead = info["description"]
    if lead and generic_text(lead, hit["url"], publisher, hit["lead"]):
        lead = None
    article["lead"] = lead or hit["lead"] or info["text"][:400]
    article["publisher"] = info["publisher"] or hit["publisher"]
    article["published_at"] = info["published_at"] or hit["published_at"]
    for key in ("text", "image", "image_alt", "image_width", "image_height"):
        article[key] = info[key]
    return article


# --- Ranking ----------------------------------------------------------------------------


def relevance(article: dict, query_words: set[str], now: datetime) -> float:
    if not query_words:
        return 0.0
    title, lead = words(article["title"]), words(article["lead"])
    score = 2 * len(query_words & title) / len(query_words)
    score += len(query_words & lead) / len(query_words)
    if article.get("published_at"):
        age_hours = (
            now - datetime.fromisoformat(article["published_at"])
        ).total_seconds() / 3600
        score += 0.75 * max(0.0, 1 - max(age_hours, 0) / 168)
    return score


def same_story(a: dict, b: dict) -> bool:
    if canonical(a["url"]) == canonical(b["url"]):
        return True
    ta, tb = words(a["title"]), words(b["title"])
    if not ta or not tb:
        return False
    return len(ta & tb) / len(ta | tb) >= 0.6


def rank(articles: list[dict], query: str, now: datetime | None = None) -> list[dict]:
    """Most relevant first; syndicated copies of one story kept once; publishers mixed."""
    now = now or datetime.now(UTC)
    query_words = words(query)
    real = [a for a in articles if not generic_text(a["title"], a["url"], a.get("publisher"), None)]
    articles = real or articles
    scored = [(relevance(a, query_words, now), i, a) for i, a in enumerate(articles)]
    # Drop articles that share no word with the query when others do.
    on_topic = [
        x for x in scored if query_words & (words(x[2]["title"]) | words(x[2]["lead"]))
    ]
    scored = on_topic or scored
    unique, urls = [], set()
    for score, i, article in sorted(scored, key=lambda x: (-x[0], x[1])):
        url = canonical(article["url"])
        if url not in urls and not any(same_story(article, kept) for _, _, kept in unique):
            unique.append((score, i, article))
        urls.add(url)
    # Light diversity: each repeat of a publisher costs a little, re-ranked greedily.
    ranked, seen = [], {}
    pool = list(unique)
    while pool:
        best = max(
            pool,
            key=lambda x: (
                x[0]
                - 0.15 * seen.get(x[2]["publisher"] or publisher_of(x[2]["url"]), 0),
                -x[1],
            ),
        )
        pool.remove(best)
        name = best[2]["publisher"] or publisher_of(best[2]["url"])
        seen[name] = seen.get(name, 0) + 1
        ranked.append(best[2])
    return newest_first_on_topic(ranked, query_words)


def topical(article: dict, query_words: set[str]) -> float:
    if not query_words:
        return 0.0
    title, lead = words(article["title"]), words(article["lead"])
    return (2 * len(query_words & title) + len(query_words & lead)) / len(query_words)


def newest_first_on_topic(ranked: list[dict], query_words: set[str]) -> list[dict]:
    """News questions want the latest development: among articles at least half as
    on-topic as the best, the most recently published one leads."""
    if len(ranked) < 2:
        return ranked
    best = max(topical(a, query_words) for a in ranked)
    close = [a for a in ranked if a.get("published_at") and topical(a, query_words) >= best / 2]
    if not close:
        return ranked
    newest = max(close, key=lambda a: a["published_at"])
    return [newest] + [a for a in ranked if a is not newest]


# --- Image ------------------------------------------------------------------------------

LOGOISH = re.compile(
    r"logo|favicon|icon|sprite|placeholder|default|blank|avatar", re.IGNORECASE
)


def valid_image(
    url: str | None, width: int | None = None, height: int | None = None
) -> bool:
    if not url or not fetchable(url):
        return False
    host = urlsplit(url).hostname or ""
    if host == "localhost" or host.endswith((".localhost", ".local", ".internal")):
        return False
    try:
        if not ipaddress.ip_address(host.strip("[]")).is_global:
            return False
    except ValueError:
        pass  # a name, not an address
    if LOGOISH.search(urlsplit(url).path):
        return False
    too_small = (width is not None and width < 300) or (
        height is not None and height < 150
    )
    return not too_small


def pick_image(ranked: list[dict]) -> tuple[str, str, str] | None:
    """(image_url, alt text, article url) from the most relevant article with one."""
    for a in ranked:
        if valid_image(a.get("image"), a.get("image_width"), a.get("image_height")):
            return a["image"], a.get("image_alt") or a["title"], a["url"]
    return None


# --- Summary ----------------------------------------------------------------------------

SYSTEM_PROMPT = (
    "You summarize news coverage for a heads-up display. You are given numbered news "
    "articles. Write 2 to 4 plain sentences, at most 600 characters in total, stating "
    "only facts that appear in these articles. Do not speculate, predict, or add "
    "background knowledge. Do not write any number, date or amount that is not in the "
    "articles. If the articles disagree, say so briefly. No markdown, lists, headings, "
    "citations or preamble: reply with the sentences only."
)
NUMBER = re.compile(r"\d[\d,.]*")
SENTENCE = re.compile(r"(?<=[.!?])\s+(?=[A-Z0-9\"“'‘(])")


def source_text(articles: list[dict]) -> str:
    """The LLM's whole input: each top article's own extracted text, nothing else."""
    blocks = []
    for n, a in enumerate(articles[:LLM_ARTICLES], 1):
        body = a.get("text") or a.get("lead") or ""
        blocks.append(
            f"[{n}] {a['publisher'] or publisher_of(a['url'])}: {a['title']}\n"
            f"{body[:LLM_CHARS_PER_ARTICLE]}"
        )
    return "\n\n".join(blocks)


ABBREVIATION = re.compile(
    r"(?:\b(?:Jan|Feb|Mar|Apr|Jun|Jul|Aug|Sep|Sept|Oct|Nov|Dec|No|Nos|Mr|Mrs|Ms|Dr|St|"
    r"Jr|Sr|vs|Gov|Sen|Rep|Reps|Gen|Lt|Col|Capt|Sgt|Det|Cpl|Adm|Cmdr|Supt|Insp|Rev|Prof|Pres|Atty|Inc|Corp|Co|Ltd|Ave|Mt|approx|est)"
    r"|\b(?:[A-Z]\.){1,3}|\b(?:a\.m|p\.m|e\.g|i\.e))\.?$"
)


def sentences(text: str) -> list[str]:
    parts = [s.strip() for s in SENTENCE.split(" ".join(text.split())) if s.strip()]
    merged: list[str] = []
    for part in parts:
        # "on Saturday, Oct." + "18 at ..." is one sentence, not two.
        if merged and ABBREVIATION.search(merged[-1]):
            merged[-1] = f"{merged[-1]} {part}"
        else:
            merged.append(part)
    return merged


def fit(parts: list[str], limit: int = SUMMARY_LIMIT) -> str:
    """Whole sentences, at most four, within `limit` characters."""
    out = ""
    for s in parts[:4]:
        candidate = f"{out} {s}".strip()
        if len(candidate) > limit:
            break
        out = candidate
    return out or parts[0][: limit - 1].rstrip() + "…"


def numbers_supported(summary: str, sources: str) -> bool:
    found = {n.rstrip(".,").replace(",", "") for n in NUMBER.findall(sources)}
    return all(
        n.rstrip(".,").replace(",", "") in found for n in NUMBER.findall(summary)
    )


async def llm_summary(articles: list[dict]) -> str | None:
    if not llm.has_key():
        return None
    text = source_text(articles)
    answer = await llm.complete(SYSTEM_PROMPT, text)
    if not answer:
        return None
    answer = re.sub(r"[*_#`>]+", "", answer)
    parts = [s for s in sentences(answer) if not s.startswith(("-", "•"))]
    if not parts:
        return None
    summary = fit(parts)
    # Never pass on a number the articles do not contain.
    return summary if numbers_supported(summary, text) else None


def extractive_summary(articles: list[dict]) -> str:
    """The top articles' lead sentences, one story fact per sentence, verbatim."""
    picked: list[str] = []
    for i, a in enumerate(articles[:LLM_ARTICLES]):
        lead_sentences = sentences(a.get("lead") or a.get("text") or "")
        for s in lead_sentences[: 2 if i == 0 else 1]:
            # Search snippets are often cut off mid-sentence ("..."): those are skipped.
            if len(s) < 15 or s[-1] not in ".!?\"”'’" or s.endswith(("...", "…")):
                continue
            if any(len(words(s) & words(p)) / max(len(words(s) | words(p)), 1) >= 0.5
                   for p in picked):  # fmt: skip
                continue
            picked.append(s)
        if len(picked) >= 3:
            break
    if not picked:
        return articles[0]["title"] + "."
    return fit(picked)


# --- Agent ------------------------------------------------------------------------------


# --- News brief -------------------------------------------------------------------------
# "Brief me on the news" has no topic: cover the day's top stories across sections.

BRIEF_REQUEST = re.compile(
    r"\bbrief(?:ing)?\b|\brundown\b|\btop\s+(?:stories|headlines|news)\b|\bheadlines\b"
    r"|\bwhat(?:'s|\s+has)?\s+happen(?:ed|ing)\b|\beverything\s+(?:that\s+)?happened\b"
    r"|\bcurrent\s+events\b|\bnews\s+(?:today|tonight|this\s+morning|yesterday)\b"
    r"|^\s*(?:the\s+)?news\s*[?.!]*\s*$",
    re.IGNORECASE,
)
# What may remain of a brief request once phrasing is removed: no real topic.
GENERAL_LEFTOVER = re.compile(
    r"^(?:(?:me|us|on|in|the|a|an|of|for|from|about|and|everything|that|all|what|whats|"
    r"what's|happened|happening|has|have|news|headlines?|stories|top|today|todays|today's|"
    r"yesterday|yesterdays|yesterday's|tonight|this|morning|week|day|brief|briefing|rundown|"
    r"summary|summarize|recap|quick|daily|current|events|latest|biggest|important|major|"
    r"united|states|u\.?s\.?a?|usa|america|american|national|world|global|international|"
    r"country|please|give|tell|catch|up|on|so|far|to|or|i|i'd|want|need|like|can|could|would|"
    r"you|show|get|let|know|whole|overall|happen|going|around|across|go|over|recent|recently)"
    r"\b[\s,.!?']*)*$",
    re.IGNORECASE,
)
SECTIONS = (
    ("Top stories", "top news"),
    ("U.S.", "U.S. news"),
    ("World", "world news"),
    ("Politics", "politics news"),
    ("Business", "business economy markets news"),
    ("Technology", "technology news"),
    ("Science & health", "science health news"),
    ("Sports", "sports news"),
)
BRIEF_STORIES = 8
POOL = "Breaking"  # a shared search whose hits only add coverage across sections
STORY_SUMMARY_LIMIT = 320


def brief_scope(text: str) -> str | None:
    """'us', 'world' or 'all' for a general news request; None when it names a topic."""
    if not BRIEF_REQUEST.search(text):
        return None
    if not GENERAL_LEFTOVER.match(" ".join(text.split())):
        return None
    if re.search(r"\b(?:united\s+states|u\.?s\.?a?|america|american|national)\b", text, re.I):
        return "us"
    if re.search(r"\b(?:world|global|international)\b", text, re.I):
        return "world"
    return "all"


def brief_sections(scope: str) -> list[tuple[str, str]]:
    sections = list(SECTIONS)
    if scope == "us":
        # U.S. focus: drop World, and keep the other sections about the U.S.
        sections = [(name, query if name in {"Top stories", "U.S."} else f"U.S. {query}")
                    for name, query in sections if name != "World"]
    elif scope == "world":
        sections = [s for s in sections if s[0] != "U.S."]
    return sections


async def _section_hits(query: str, timelimit: str) -> list[dict]:
    try:
        return await ddgs_news(query, timelimit)
    except (SearchUnavailable, aiohttp.ClientError, TimeoutError, ValueError):
        try:
            return await web_news(query)
        except (SearchUnavailable, aiohttp.ClientError, TimeoutError, ValueError):
            return []


# Section fronts, listicles, tools and evergreen pages are not stories.
NOT_A_STORY = re.compile(
    r"^(?:top|best)\s+\d+\b|\bcompare\b|readers.?\s*choice|\bhow\s+to\b|\bguide\s+to\b|"
    # Shopping deal pages only: "ceasefire deal" and "trade deal" are news.
    r"\b(?:best|top|early|daily|black\s+friday|cyber\s+monday|prime\s+day|holiday)\s+deals?\b|"
    r"\bdeals?\s+(?:on|of\s+the\s+(?:day|week))\b|\bcoupons?\b|\bthis\s+week\s+on\b|\bwhat\s+to\s+watch\b|"
    r"\b(?:latest|breaking|top)\s+(?:\w+\s+){0,3}(?:news|headlines)\b.*\|"
    r"|\|\s*(?:\w+\s+){0,3}(?:news|headlines)\s*(?:\||$)|^(?:news|headlines|home)\b",
    re.IGNORECASE,
)
NEWSROOMS = re.compile(
    r"\b(?:ap|associated press|reuters|npr|bbc|cnn|nbc|cbs|abc|pbs|new york times|nytimes|"
    r"wall street journal|wsj|washington post|bloomberg|politico|axios|usa today|guardian|"
    r"fox news|espn|the hill|los angeles times|latimes|cnbc|financial times|ft\.com|"
    r"the atlantic|time|newsweek|al jazeera)\b",
    re.IGNORECASE,
)


def newsworthy(hit: dict, now: datetime, max_hours: float) -> bool:
    """A dated, recent article, not a section front, listicle or tool page."""
    if not hit.get("published_at") or NOT_A_STORY.search(hit["title"]):
        return False
    hours = (now - datetime.fromisoformat(hit["published_at"])).total_seconds() / 3600
    return -6 <= hours <= max_hours


def _story_score(cluster: list[dict], now: datetime) -> float:
    """More outlets covering it, recent coverage and established newsrooms rank higher."""
    publishers = {a["publisher"] or publisher_of(a["url"]) for a in cluster}
    newest = max((a["published_at"] for a in cluster if a.get("published_at")), default=None)
    recency = 0.0
    if newest:
        hours = (now - datetime.fromisoformat(newest)).total_seconds() / 3600
        recency = max(0.0, 1 - max(hours, 0) / 48)
    newsroom = 0.5 if any(NEWSROOMS.search(f"{p} {a['url']}") for p in publishers for a in cluster[:1]) else 0.0
    return len(publishers) + recency + newsroom


# Section by what the story is about; the search that found it is only a fallback.
TOPICS = (
    ("Sports", r"\b(?:nfl|nba|mlb|nhl|mls|ncaa|football|basketball|baseball|hockey|soccer|"
               r"tennis|golf|playoffs?|quarterback|coach|touchdown|world\s+cup|olympic|"
               r"championship|league|season|game|match|ranked|rankings?|poll)\b"),
    ("Politics", r"\b(?:president|vice\s+president|trump|vance|biden|congress|senate|"
                 r"senator|house|governor|election|campaign|democrats?|republicans?|gop|"
                 r"white\s+house|supreme\s+court|lawmakers?|ballot|voters?|town\s+hall)\b"),
    ("World", r"\b(?:ukraine|russia|china|iran|israel|gaza|europe|nato|united\s+nations|"
              r"war|strike|missile|troops|military|embassy|foreign|strait)\b"),
    ("Business", r"\b(?:stocks?|markets?|economy|economic|inflation|fed|interest\s+rates?|"
                 r"earnings|shares|company|companies|ceo|tariffs?|jobs\s+report|wall\s+street|"
                 r"prices|retail|bank)\b"),
    ("Technology", r"\b(?:ai|artificial\s+intelligence|tech|apple|google|microsoft|meta|"
                   r"openai|software|chip|chips|smartphone|starlink|spacex|cyber|startup)\b"),
    ("Science & health", r"\b(?:health|disease|virus|vaccine|cdc|fda|study|scientists?|"
                         r"research|nasa|space|climate|hospital|medical|drug)\b"),
)
MAX_PER_SECTION = 2
TOPIC_WORDS = re.compile(r"[A-Za-z][A-Za-z'-]+")


def section_of(cluster: list[dict], fallback: str) -> str:
    text = " ".join(f"{a['title']} {a.get('lead') or ''}" for a in cluster[:3])
    hits = [(len(re.findall(pattern, text, re.IGNORECASE)), name) for name, pattern in TOPICS]
    count, name = max(hits)
    return name if count else fallback


def related(a: dict, b: dict) -> bool:
    """The same event in different outlets' words: 3+ shared key words, enough overlap."""
    if same_story(a, b):
        return True
    wa, wb = words(a["title"]), words(b["title"])
    shared = wa & wb
    return len(shared) >= 3 and len(shared) / max(len(wa | wb), 1) >= 0.25


def readable(hit: dict) -> bool:
    """English news, not opinion columns."""
    title = hit["title"]
    if re.match(r"^\s*(?:opinion|op-ed|editorial|commentary|letters?)\b", title, re.IGNORECASE):
        return False
    latin = sum(ch.isascii() for ch in title)
    return latin / max(len(title), 1) >= 0.85


def pick_stories(
    section_hits: list[tuple[str, list[dict]]], now: datetime | None = None, max_hours: float = 36
) -> list[dict]:
    """One cluster per story across all searches; the most important stories, balanced
    across sections (at most two each), no repeats."""
    now = now or datetime.now(UTC)
    clusters: list[dict] = []
    for search_section, hits in section_hits:
        for hit in hits:
            if not hit["title"] or generic_text(hit["title"], hit["url"], hit["publisher"], None):
                continue
            if not newsworthy(hit, now, max_hours) or not readable(hit):
                continue
            home = next((c for c in clusters if any(related(hit, a) for a in c["items"])), None)
            if home is None:
                clusters.append({"items": [hit], "found_in": search_section})
            elif all(canonical(a["url"]) != canonical(hit["url"]) for a in home["items"]):
                home["items"].append(hit)
    for c in clusters:
        fallback = "Top stories" if c["found_in"] in {POOL, "U.S."} else c["found_in"]
        c["section"] = section_of(c["items"], fallback)
        c["score"] = _story_score(c["items"], now)
    ranked = sorted(clusters, key=lambda c: -c["score"])
    picked, per = [], {}
    # First the best story of each section, in section order, then the rest by importance.
    order = [name for name, _ in TOPICS] + ["Top stories"]
    firsts = [next((c for c in ranked if c["section"] == name), None) for name in order]
    for cluster in [c for c in firsts if c] + ranked:
        if len(picked) >= BRIEF_STORIES:
            break
        if cluster in picked or per.get(cluster["section"], 0) >= MAX_PER_SECTION:
            continue
        per[cluster["section"]] = per.get(cluster["section"], 0) + 1
        picked.append(cluster)
    picked.sort(key=lambda c: -c["score"])  # most important first
    return [{"section": c["section"], "cluster": c["items"]} for c in picked]


def story_summary(article: dict) -> str:
    """One or two of the article's own lead sentences, whole, within the limit."""
    lead = article.get("lead") or article.get("text") or ""
    # Search snippets often start with their age: "3 hours ago · ...".
    lead = re.sub(r"^\s*(?:\d+\s+\w+\s+ago|[A-Z][a-z]{2}\s+\d{1,2},\s+\d{4})\s*[·\-–]\s*", "", lead)
    article = {**article, "lead": lead}
    parts = [
        s for s in sentences(article.get("lead") or article.get("text") or "")
        if len(s) >= 25 and s[-1] in ".!?\"”'’" and not s.endswith(("...", "…"))
    ]
    out = ""
    for s in parts[:2]:
        candidate = f"{out} {s}".strip()
        if len(candidate) > STORY_SUMMARY_LIMIT:
            break
        out = candidate
    if out:
        return out
    lead = article.get("lead") or ""
    # No whole sentence fits: a trimmed lead, marked as cut.
    return lead[: STORY_SUMMARY_LIMIT - 1].rstrip(" .") + "…" if lead else ""


STORY_BODY_LIMIT = 900
STORY_BODY_SENTENCES = 6
# Page furniture that is not part of the story.
BOILERPLATE = re.compile(
    r"\b(?:subscribe|sign\s+up|newsletter|advertisement|click\s+here|read\s+more|"
    r"all\s+rights\s+reserved|copyright|follow\s+us|contributed\s+to\s+this\s+report|"
    r"this\s+(?:story|article)\s+(?:has\s+been|was)\s+updated|getty\s+images|cookies?|"
    r"terms\s+of\s+(?:use|service)|privacy\s+policy|log\s+in|download\s+the\s+app|"
    r"watch\s+(?:the\s+)?video|listen\s+to\s+this|share\s+this|related\s*:|photo\s*:)\b",
    re.IGNORECASE,
)
STORY_PROMPT = (
    "You write one paragraph of a daily news newsletter. You are given the text of news "
    "articles about one story. Write 4 to 6 plain sentences, at most 900 characters, that "
    "explain what happened, who is involved, and why it matters, using only facts stated in "
    "the articles. Do not speculate or add background knowledge. Do not write any number, "
    "date or amount that is not in the articles. If the articles disagree, say so. No "
    "markdown, lists or headings."
)


AGE_PREFIX = re.compile(r"^\s*(?:\d+\s+\w+\s+ago|[A-Z][a-z]{2}\s+\d{1,2},\s+\d{4})\s*[·\-–]\s*")
# Photo captions and credits: "This image provided by KAKE shows...", "(AP Photo/...)".
CAPTION = re.compile(
    r"\b(?:image|photo|picture)\s+(?:provided|taken|shows)\b|\bprovided\s+by\b.*\bshows\b|"
    r"\(\s*(?:ap\s+photo|credit|photo|getty|reuters)\b|\bvia\s+ap\)|/\s*(?:ap|getty|reuters)\b",
    re.IGNORECASE,
)


CALENDAR = {
    "monday", "tuesday", "wednesday", "thursday", "friday", "saturday", "sunday", "today",
    "yesterday", "january", "february", "march", "april", "may", "june", "july", "august",
    "september", "october", "november", "december", "jan", "feb", "mar", "apr", "jun",
    "jul", "aug", "sep", "sept", "oct", "nov", "dec", "said", "says", "year", "week",
}  # fmt: skip


def on_story(sentence: str, title: str, story_words: set[str]) -> bool:
    """About this story: shares a headline word, or two words with the story's opening."""
    mine = words(sentence) - CALENDAR
    return bool(mine & (words(title) - CALENDAR)) or len(mine & (story_words - CALENDAR)) >= 2


def key_sentences(texts: list[str], title: str) -> str:
    """The most informative sentences across the articles, in reading order."""
    # Paragraphs that don't end like a sentence are "read also" links and subheads;
    # left in, they glue onto the next sentence ("...In Ruins Barate called it...").
    texts = [
        " ".join(p for p in (AGE_PREFIX.sub("", p).strip() for p in t.split("\n"))
                 if p and p[-1] in ".!?\"”'’)")
        for t in texts
    ]
    # The story's vocabulary: its headline and each article's first two sentences.
    story_words = words(title)
    for text in texts:
        for s in sentences(text)[:2]:
            story_words |= words(s)
    pool: list[tuple[int, int, str]] = []  # (article, position, sentence)
    for a, text in enumerate(texts):
        for i, s in enumerate(sentences(text)):
            if not 8 <= len(s.split()) <= 60 or BOILERPLATE.search(s) or CAPTION.search(s):
                continue
            letters = [c for c in s if c.isalpha()]
            if letters and sum(c.isupper() for c in letters) / len(letters) > 0.6:
                continue  # an all-caps teaser headline for another story
            # Pages often append other stories; past the opening, stay on this one.
            if i >= 2 and not on_story(s, title, story_words):
                continue
            if s[-1] not in ".!?\"”'’" or s.endswith(("...", "…")):
                continue
            if any(len(words(s) & words(p)) / max(len(words(s) | words(p)), 1) >= 0.5
                   for _, _, p in pool):  # fmt: skip
                continue  # the second outlet repeating the first
            pool.append((a, i, s))
    if not pool:
        return ""
    freq: dict[str, int] = {}
    for _, _, s in pool:
        for w in words(s):
            freq[w] = freq.get(w, 0) + 1
    title_words = words(title)

    def score(entry):
        a, i, s = entry
        ws = words(s)
        topical = sum(freq[w] for w in ws) / (len(ws) ** 0.5 + 1)
        return topical + 2 * len(ws & title_words) + max(0, 3 - i) * 0.6 - a * 0.3

    chosen = sorted(sorted(pool, key=score, reverse=True)[:STORY_BODY_SENTENCES],
                    key=lambda e: (e[0], e[1]))
    out = ""
    for _, _, s in chosen:
        candidate = f"{out} {s}".strip()
        if len(candidate) > STORY_BODY_LIMIT:
            continue
        out = candidate
    return out


async def story_body(article: dict) -> tuple[str, list[str]]:
    """(newsletter paragraph, outlets read) from the story's full article text."""
    texts = [(name, text) for name, text in article.get("texts") or [] if text]
    if not texts:
        return "", []
    outlets = list(dict.fromkeys(name for name, _ in texts))
    if llm.has_key():
        source = "\n\n".join(f"[{n}] {name}:\n{text[:LLM_CHARS_PER_ARTICLE]}"
                             for n, (name, text) in enumerate(texts, 1))  # fmt: skip
        answer = await llm.complete(STORY_PROMPT, source)
        if answer:
            answer = " ".join(re.sub(r"[*_#`>]+", "", answer).split())[:STORY_BODY_LIMIT]
            if len(answer) > 80 and numbers_supported(answer, source):
                return answer, outlets
    return key_sentences([text for _, text in texts], article["title"]), outlets


def brief_title(scope: str, text: str, now: datetime) -> str:
    day = now - timedelta(days=1) if re.search(r"\byesterday\b", text, re.I) else now
    where = {"us": "U.S. news brief", "world": "World news brief"}.get(scope, "News brief")
    return f"{where}: {day.strftime('%A, %B')} {day.day}"


# --- Google News feeds ------------------------------------------------------------------
# Google News' public RSS feeds are editor-ranked by section and list the other outlets
# covering each story, which makes them the brief's story selection. Only these fixed
# news.google.com URLs are requested; article details come from a news search per story.
GNEWS = "https://news.google.com/rss"
GNEWS_EDITION = {"hl": "en-US", "gl": "US", "ceid": "US:en"}
GNEWS_SECTIONS = (
    ("Top stories", None),
    ("U.S.", "NATION"),
    ("World", "WORLD"),
    ("Business", "BUSINESS"),
    ("Technology", "TECHNOLOGY"),
    ("Science", "SCIENCE"),
    ("Health", "HEALTH"),
    ("Sports", "SPORTS"),
)
_lookups = asyncio.Semaphore(3)  # news searches per brief, to stay under rate limits


def parse_gnews(xml: str) -> list[dict]:
    from xml.etree import ElementTree

    try:
        root = ElementTree.fromstring(xml)
    except ElementTree.ParseError:
        return []
    items = []
    for item in root.findall("./channel/item"):
        publisher = " ".join((item.findtext("source") or "").split()) or None
        title = " ".join((item.findtext("title") or "").split())
        if publisher and title.endswith(f" - {publisher}"):
            title = title[: -len(publisher) - 3]
        # "Japan to launch Martian moon mission | NHK WORLD-JAPAN News": drop the site tag.
        title = re.sub(r"\s+\|\s+[^|]{2,60}\b(?:news|times|post|world|today)\s*$", "", title, flags=re.I)
        try:
            published = parsedate_to_datetime(item.findtext("pubDate") or "").astimezone(UTC).isoformat()
        except (TypeError, ValueError):
            published = None
        link = item.findtext("link") or ""
        if not title or not link.startswith("https://news.google.com/"):
            continue
        items.append({
            "url": link, "title": title[:200], "lead": "", "publisher": publisher,
            "published_at": published,
            # The description lists related coverage from other outlets.
            "coverage": max(1, (item.findtext("description") or "").count("<li>")),
        })
    return items


async def gnews_feed(topic: str | None) -> list[dict]:
    url = GNEWS if topic is None else f"{GNEWS}/headlines/section/topic/{topic}"
    params = GNEWS_EDITION
    try:
        # Section feeds redirect to /rss/topics/...; follow only within news.google.com.
        for _ in range(3):
            async with net.session().get(
                url, params=params, headers={"User-Agent": HEADERS["User-Agent"]},
                timeout=aiohttp.ClientTimeout(total=10), allow_redirects=False,
            ) as response:
                if response.status in {301, 302, 303, 307, 308}:
                    url, params = urljoin(url, response.headers.get("Location", "")), None
                    if not url.startswith("https://news.google.com/"):
                        return []
                    continue
                if response.status != 200:
                    return []
                return parse_gnews(await response.text())
        return []
    except (aiohttp.ClientError, TimeoutError, ValueError):
        return []


def gnews_sections(scope: str) -> list[tuple[str, str | None]]:
    if scope == "us":
        return [s for s in GNEWS_SECTIONS if s[0] != "World"]
    if scope == "world":
        return [s for s in GNEWS_SECTIONS if s[0] != "U.S."]
    return list(GNEWS_SECTIONS)


def pick_feed_stories(feeds: list[tuple[str, list[dict]]], now: datetime, max_hours: float) -> list[dict]:
    """Editor order within each section: two top stories, then one per section, then more."""
    picked: list[dict] = []
    queues = {name: [i for i in items if newsworthy(i, now, max_hours) and readable(i)]
              for name, items in feeds}
    # A U.S. brief leads with U.S. stories; otherwise the general top stories lead.
    lead_section = "U.S." if any(name == "U.S." for name, _ in feeds) and not any(
        name == "World" for name, _ in feeds) else "Top stories"
    quotas = [(name, 2 if name == lead_section else 1) for name, _ in feeds]
    quotas.sort(key=lambda q: q[0] != lead_section)
    for round_quota in (quotas, [(name, 1) for name, _ in feeds]):
        for name, quota in round_quota:
            taken = 0
            while queues[name] and taken < quota and len(picked) < BRIEF_STORIES:
                item = queues[name].pop(0)
                if any(related(item, p["item"]) for p in picked):
                    continue
                picked.append({"section": name, "item": item})
                taken += 1
    return picked


async def find_article(item: dict) -> dict:
    """The story's own article (direct link, lead, image) via a news search on its headline."""
    match = None
    for lookup in (lambda: ddgs_news(item["title"], "d"), lambda: web_news(item["title"])):
        async with _lookups:
            try:
                hits = await lookup()
            except (SearchUnavailable, aiohttp.ClientError, TimeoutError, ValueError):
                hits = []
        match = next((h for h in hits if related(h, item)), None)
        if match:
            break
    if match is None:
        return {**item, "text": "", "image": None, "image_alt": None,
                "image_width": None, "image_height": None, "texts": []}  # fmt: skip
    # A second outlet's article on the same story widens the facts the summary draws on.
    first_outlet = match["publisher"] or publisher_of(match["url"])
    second = next(
        (h for h in hits if h is not match and related(h, item)
         and (h["publisher"] or publisher_of(h["url"])) != first_outlet),
        None,
    )
    article, *more = await asyncio.gather(read_article(match), *([read_article(second)] if second else []))
    texts = [
        (a["publisher"] or publisher_of(a["url"]), a.get("text") or a.get("lead") or "")
        for a in [article, *more]
    ]
    # Keep the feed's headline, outlet and time; article pages sometimes carry other dates.
    return {**article, "title": item["title"], "publisher": item["publisher"] or article["publisher"],
            "published_at": item["published_at"] or article["published_at"], "texts": texts}


async def feed_brief(scope: str, now: datetime, max_hours: float) -> list[dict]:
    """[{"section", "item", "article"}] from Google News, or [] if the feeds fail."""
    sections = gnews_sections(scope)
    feeds = await asyncio.gather(*(gnews_feed(topic) for _, topic in sections))
    named = [(name, items) for (name, _), items in zip(sections, feeds) if items]
    if not named:
        return []
    picked = pick_feed_stories(named, now, max_hours)
    report("reading", f"Finding the articles for {len(picked)} top stories")
    articles = await asyncio.gather(*(find_article(p["item"]) for p in picked))
    return [{**p, "article": a} for p, a in zip(picked, articles)]


async def news_brief(req: NewsRequest, scope: str) -> NewsResponse:
    now = datetime.now(UTC)
    yesterday = bool(re.search(r"\byesterday\b", req.query, re.I))
    max_hours = 60 if yesterday else 36
    report("searching", "Collecting today's top stories by section")
    from_feeds = await feed_brief(scope, now, max_hours)
    if from_feeds:
        return await brief_response(req, scope, now, [
            (p["section"], p["article"], p["item"]["coverage"]) for p in from_feeds
        ])
    sections = brief_sections(scope)
    report("searching", f"Searching today's top stories across {len(sections)} sections")
    searches = [*sections, (POOL, "breaking news today")]
    results = await asyncio.gather(*(_section_hits(q, "d") for _, q in searches))
    section_hits = [(name, hits) for (name, _), hits in zip(searches, results) if hits]
    if not section_hits:
        return NewsResponse(
            query=req.query,
            brief=True,
            error="News search unavailable",
            summary="News search is unavailable right now; no brief was put together.",
        )
    picked = pick_stories(section_hits, now, max_hours=max_hours)
    report("reading", f"Reading {len(picked)} lead articles")
    leads = await asyncio.gather(
        *(read_article(max(p["cluster"], key=lambda a: len(a.get("lead") or ""))) for p in picked)
    )
    coverage = [len({a["publisher"] or publisher_of(a["url"]) for a in p["cluster"]}) for p in picked]
    for article in leads:
        article["texts"] = [(article["publisher"] or publisher_of(article["url"]),
                             article.get("text") or article.get("lead") or "")]
    return await brief_response(
        req, scope, now, [(p["section"], a, c) for p, a, c in zip(picked, leads, coverage)]
    )


async def brief_response(
    req: NewsRequest, scope: str, now: datetime, items: list[tuple[str, dict, int]]
) -> NewsResponse:
    report("summarizing", f"Writing summaries of {len(items)} stories from their full articles")
    bodies = await asyncio.gather(*(story_body(article) for _, article, _ in items))
    stories = []
    for (section, article, coverage), (body, read_from) in zip(items, bodies):
        if not article["title"]:
            continue
        image = article.get("image") if valid_image(
            article.get("image"), article.get("image_width"), article.get("image_height")
        ) else None
        stories.append(
            NewsStory(
                section=section,
                headline=article["title"][:200],
                summary=story_summary(article),
                url=article["url"],
                publisher=article["publisher"] or publisher_of(article["url"]),
                published_at=article["published_at"],
                image_url=image,
                coverage=coverage,
                body=body,
                read_from=read_from[:3],
            )
        )
    if not stories:
        return NewsResponse(query=req.query, brief=True, summary="No recent top stories were found.")
    title = brief_title(scope, req.query, now)
    overview = f"{title}. " + " ".join(f"{s.section}: {s.headline}." for s in stories[:4])
    lead_image = next((s for s in stories if s.image_url), None)
    return NewsResponse(
        query=req.query,
        brief=True,
        headline=title,
        summary=overview[:SUMMARY_LIMIT],
        image_url=lead_image.image_url if lead_image else None,
        image_alt=lead_image.headline if lead_image else None,
        image_source_url=lead_image.url if lead_image else None,
        stories=stories,
        sources=[
            NewsSource(title=s.headline, url=s.url, publisher=s.publisher, published_at=s.published_at)
            for s in stories[:MAX_SOURCES_OUT]
        ],
    )

def chat_reply(result: NewsResponse) -> str:
    """Plain text: headline, summary, then up to three source links."""
    if result.brief and result.stories:
        lines = [result.headline or "News brief"]
        for story in result.stories:
            lines.append(f"{story.section}: {story.headline}. {story.body or story.summary} ({story.publisher})")
        return "\n".join(lines)
    lines = []
    if result.headline:
        lines.append(result.headline)
    if result.summary:
        lines.append(result.summary)
    for s in result.sources[:3]:
        lines.append(f"{s.publisher}: {s.url}")
    return "\n".join(lines) or "No news found."


@tracked("news")
async def summarize_news(req: NewsRequest) -> NewsResponse:
    scope = brief_scope(req.query)
    if scope:
        return await news_brief(req, scope)
    query = parse_query(req.query)
    report("searching", f"Searching news for {query}")
    hits = await find_hits(query)
    if hits is None:
        return NewsResponse(
            query=query,
            error="News search unavailable",
            summary="News search is unavailable right now; nothing was summarized.",
        )
    unique, seen = [], set()
    for hit in hits:
        if canonical(hit["url"]) not in seen:
            seen.add(canonical(hit["url"]))
            unique.append(hit)
    if not unique:
        return NewsResponse(query=query, summary=f"No recent news found for {query}.")
    picked = unique[: req.max_sources]
    report("reading", f"Reading {len(picked)} articles")
    articles = await asyncio.gather(*(read_article(h) for h in picked))
    report("ranking", "Ranking articles by relevance")
    ranked = [a for a in rank(list(articles), query) if a["title"]]
    if not ranked:
        return NewsResponse(query=query, summary=f"No recent news found for {query}.")
    report("summarizing", f"Summarizing {min(len(ranked), LLM_ARTICLES)} articles")
    summary = await llm_summary(ranked) or extractive_summary(ranked)
    image = pick_image(ranked)
    return NewsResponse(
        query=query,
        headline=ranked[0]["title"],
        summary=summary,
        image_url=image[0] if image else None,
        image_alt=image[1] if image else None,
        image_source_url=image[2] if image else None,
        sources=[
            NewsSource(
                title=a["title"],
                url=a["url"],
                publisher=a["publisher"] or publisher_of(a["url"]),
                published_at=a["published_at"],
            )
            for a in ranked[:MAX_SOURCES_OUT]
        ],
    )
