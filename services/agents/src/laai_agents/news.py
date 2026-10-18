"""News summary: a short, sourced overview of recent coverage for a topic.

Search is Tavily news search when TAVILY_API_KEY is set: its results carry the article
text and images, so pages are only fetched when that text is thin or no result has a
usable image. Without a key, or when Tavily fails or finds nothing, search is keyless
DDGS news, then the shared web search. The top results are read
concurrently (HTTPS only, public hosts only, 8 s and 1.5 MB per page, redirects
re-checked), ranked by how well their title and lead match the query plus recency, and
near-duplicate syndicated copies are dropped. The summary comes from ASI:One when a key
is set, given only the extracted text of the top articles; otherwise it is built from the
articles' own lead sentences. A summary with a number the sources do not contain is
discarded for the extractive one. Nothing is guessed when nothing is found.
"""

import asyncio
import ipaddress
import os
import re
from datetime import UTC, datetime
from email.utils import parsedate_to_datetime
from urllib.parse import urljoin, urlsplit, urlunsplit

import aiohttp
from lxml import html as lxml_html

from . import llm, net
from .merchants import fetchable, public_host
from .models import NewsRequest, NewsResponse, NewsSource
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
        "lead": " ".join(str(lead or "").split())[:600],
        "publisher": " ".join(str(publisher).split())[:80] if publisher else None,
        "published_at": normalize_date(published),
    }


async def ddgs_news(query: str) -> list[dict]:
    def run():
        from ddgs import DDGS

        return DDGS(timeout=8).news(
            query, region="us-en", safesearch="moderate", timelimit="w", max_results=20
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
PROVIDER_LABELS = {"ddgs_news": "DDGS", "web_search": "web search"}


async def find_hits(query: str) -> list[dict] | None:
    """Hits from the first provider that answers with results; None if none answered."""
    answered = False
    for name, run in PROVIDERS:
        report(
            "searching",
            f"Searching news ({PROVIDER_LABELS.get(name, name)}) for {query}",
        )
        try:
            found = await run(query)
        except (SearchUnavailable, aiohttp.ClientError, TimeoutError, ValueError):
            continue
        answered = True
        if found:
            return found
    return [] if answered else None


# --- Tavily -----------------------------------------------------------------------------

TAVILY_URL = "https://api.tavily.com/search"
TAVILY_TIMEOUT = aiohttp.ClientTimeout(total=15)
# Tavily text at least this long is enough to rank and summarize without the page.
ENOUGH_TEXT = 600


class TavilyUnavailable(RuntimeError):
    pass


def tavily_key() -> str:
    return os.getenv("TAVILY_API_KEY", "").strip()


def tavily_payload(query: str, max_results: int) -> dict:
    return {
        "query": query,
        "topic": "news",
        "time_range": "week",
        "max_results": max_results,
        # basic costs one credit; advanced costs two and has not been shown to read better.
        "search_depth": "basic",
        "include_raw_content": "text",
        "include_images": True,
        "include_image_descriptions": True,
        "include_answer": False,
    }


def _image(value) -> tuple[str | None, str | None]:
    """Tavily images are objects {url, description}; older responses used plain strings."""
    if isinstance(value, dict):
        return value.get("url"), value.get("description")
    return (value, None) if isinstance(value, str) else (None, None)


def tavily_article(result: dict) -> dict | None:
    hit = _hit(
        result.get("url"),
        result.get("title"),
        result.get("content"),
        None,
        result.get("published_date"),
    )
    if not hit:
        return None
    raw = " ".join(str(result.get("raw_content") or "").split())
    content = " ".join(str(result.get("content") or "").split())
    image, alt = None, None
    for item in result.get("images") or []:
        url, description = _image(item)
        if valid_image(url):
            image, alt = (
                url,
                " ".join(str(description).split())[:200] if description else None,
            )
            break
    return {
        **hit,
        "text": raw if len(raw) > len(content) else content,
        "image": image,
        "image_alt": alt,
        "image_width": None,
        "image_height": None,
    }


async def tavily_news(query: str, max_results: int) -> list[dict]:
    key = tavily_key()
    if not key:
        raise TavilyUnavailable("Tavily API key is not configured")
    async with net.session().post(
        TAVILY_URL,
        json=tavily_payload(query, max_results),
        headers={"Authorization": f"Bearer {key}", "Content-Type": "application/json"},
        timeout=TAVILY_TIMEOUT,
    ) as response:
        if response.status != 200:
            # Never echo the response or request: either can carry the key.
            raise TavilyUnavailable(f"Tavily returned HTTP {response.status}")
        data = await response.json()
    if not isinstance(data, dict):
        raise TavilyUnavailable("Tavily returned an unexpected response")
    return [
        article
        for result in data.get("results") or []
        if isinstance(result, dict) and (article := tavily_article(result))
    ]


TAVILY_FAILURES = (
    TavilyUnavailable,
    aiohttp.ClientError,
    TimeoutError,
    ValueError,
    TypeError,
)


async def complete_tavily(articles: list[dict]) -> list[dict]:
    """Fetch pages only for thin text, or for og:image when no result has an image."""
    thin = [i for i, a in enumerate(articles) if len(a["text"]) < ENOUGH_TEXT]
    if not any(a["image"] for a in articles):
        thin = sorted(set(thin) | set(range(min(3, len(articles)))))
    if thin:
        report("reading", f"Reading {len(thin)} articles")
        read = await asyncio.gather(*(read_article(articles[i]) for i in thin))
        for i, article in zip(thin, read, strict=True):
            articles[i] = article
    return articles


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


async def read_article(hit: dict) -> dict:
    """The search hit, filled in from its page when the page can be read."""
    article = {"text": "", "image": None, "image_alt": None,
               "image_width": None, "image_height": None, **hit}  # fmt: skip
    page = await fetch_page(hit["url"])
    if not page:
        return article
    try:
        info = extract(page, hit["url"])
    except (ValueError, TypeError):  # malformed markup
        return article
    article["title"] = info["title"] or hit["title"]
    article["lead"] = info["description"] or hit["lead"] or info["text"][:400]
    article["publisher"] = info["publisher"] or hit["publisher"]
    article["published_at"] = info["published_at"] or hit["published_at"]
    if len(info["text"]) > len(article["text"]):
        article["text"] = info["text"]
    if info["image"] and not article["image"]:
        for key in ("image", "image_alt", "image_width", "image_height"):
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
    scored = [(relevance(a, query_words, now), i, a) for i, a in enumerate(articles)]
    # Drop articles that share no word with the query when others do.
    on_topic = [
        x for x in scored if query_words & (words(x[2]["title"]) | words(x[2]["lead"]))
    ]
    scored = on_topic or scored
    unique, urls = [], set()
    for score, i, article in sorted(scored, key=lambda x: (-x[0], x[1])):
        url = canonical(article["url"])
        if url not in urls and not any(
            same_story(article, kept) for _, _, kept in unique
        ):
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
    return ranked


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


def sentences(text: str) -> list[str]:
    return [s.strip() for s in SENTENCE.split(" ".join(text.split())) if s.strip()]


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


def unique_urls(hits: list[dict]) -> list[dict]:
    unique, seen = [], set()
    for hit in hits:
        if canonical(hit["url"]) not in seen:
            seen.add(canonical(hit["url"]))
            unique.append(hit)
    return unique


def chat_reply(result: NewsResponse) -> str:
    """Plain text: headline, summary, then up to three source links."""
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
    query = parse_query(req.query)
    articles = []
    if tavily_key():
        report("searching", f"Searching news (Tavily) for {query}")
        try:
            articles = unique_urls(await tavily_news(query, req.max_sources))[
                : req.max_sources
            ]
        except TAVILY_FAILURES as exc:
            reason = (
                str(exc) if isinstance(exc, TavilyUnavailable) else "request failed"
            )
            report("fallback", f"Tavily unavailable ({reason}); using free news search")
        else:
            if not articles:
                report("fallback", "Tavily found nothing; using free news search")
        if articles:
            articles = await complete_tavily(articles)
    if not articles:
        hits = await find_hits(query)
        if hits is None:
            return NewsResponse(
                query=query,
                error="News search unavailable",
                summary="News search is unavailable right now; nothing was summarized.",
            )
        picked = unique_urls(hits)[: req.max_sources]
        if not picked:
            return NewsResponse(
                query=query, summary=f"No recent news found for {query}."
            )
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
