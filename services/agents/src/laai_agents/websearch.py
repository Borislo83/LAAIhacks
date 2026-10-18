"""General web search: a short, sourced answer to a question, by a hard deadline.

"What is X", "how do I Y", "when does the dining hall open", "best ramen near UCLA",
"weather in LA". The web is searched (the installed engines race, see
search.free_search; a few spare engines if those all come back empty), plus DDGS news when the question asks for news. The top results
are read concurrently with the news agent's reader (HTTPS, public hosts, redirects
re-checked) until the read budget runs out; a page that isn't back by then keeps its
search snippet. The answer is 2-4 sentences from ASI:One, given only the question and
the extracted text, when a key is set and time remains; any sentence with a number the
sources don't contain is dropped. Otherwise, and as the fallback, the answer is the
pages' own sentences that best match the question, verbatim. Weather questions are
answered from Open-Meteo's forecast API (fixed hosts, keyless). Recency only counts
for news-like questions. Whatever has arrived by the deadline is answered from, and
answers are cached briefly (a partial one for a minute).
"""

import asyncio
import os
import re
import time
from collections import OrderedDict

import aiohttp
import ddgs  # noqa: F401 -- imported up front so the first search doesn't pay for it

from . import llm, net, news
from .models import NewsSource, SearchRequest, SearchResponse
from .progress import report, tracked
from .search import RACE, SearchUnavailable, free_search


def _seconds(name: str, default: float) -> float:
    try:
        return max(1.0, float(os.getenv(name, default)))
    except ValueError:
        return default


# Every answer is back by this many seconds (env: SEARCH_DEADLINE_SECONDS).
DEADLINE = _seconds("SEARCH_DEADLINE_SECONDS", 5.0)
# Repeat questions are answered from memory for this long (env: SEARCH_CACHE_SECONDS);
# news and weather for at most two minutes.
CACHE_SECONDS = _seconds("SEARCH_CACHE_SECONDS", 300.0)
FRESH_CACHE_SECONDS = 120.0
PARTIAL_CACHE_SECONDS = 60.0
CACHE_SIZE = 128
SEARCH_SHARE = 0.64  # of the deadline, for finding results (one slow engine takes ~2.5 s)
READ_SECONDS = 2.0  # at most, for reading pages
LLM_MIN_SECONDS = 0.8  # less than this left: answer extractively
MAX_SOURCES_OUT = 3
ANSWER_LIMIT = 500

# --- Query ------------------------------------------------------------------------------

LEADING = [
    re.compile(p, re.IGNORECASE)
    for p in (
        r"^(?:hey|hi|ok|okay|please|pls|so|focal)\b[\s,]*",
        r"^(?:can|could|would|will)\s+you\s+(?:please\s+)?",
        (
            r"^(?:do\s+a\s+)?(?:quick\s+)?(?:web\s+|google\s+|online\s+)?search\s+"
            r"(?:the\s+web\s+|online\s+|google\s+)?(?:for\s+)?"
        ),
        r"^(?:look\s+up|google|find\s+out|tell\s+me|i\s+(?:want|need)\s+to\s+know|do\s+you\s+know)\b[\s:,]*",
    )
]
TRAILING = [
    re.compile(p, re.IGNORECASE)
    for p in (
        r"[\s?.!]+$",
        r"\s+(?:please|for\s+me|online|on\s+the\s+web|on\s+google)$",
    )
]


def clean_query(text: str) -> str:
    """'hey can you look up when Bruin Plate opens?' -> 'when Bruin Plate opens'.
    Question words stay: they help the search engines."""
    query = " ".join(text.split())
    changed = True
    while changed:
        before = query
        for pattern in LEADING + TRAILING:
            query = pattern.sub("", query)
        query = query.strip(" \"'“”‘’:,-")
        changed = query != before
    return query or " ".join(text.split())


NEWSY = re.compile(
    r"\b(?:news|latest|breaking|headlines?|updates?|recent(?:ly)?|what\s+happened|"
    r"announce(?:d|ment)?|scores?|election\s+results?)\b",
    re.IGNORECASE,
)


def news_like(query: str) -> bool:
    return bool(NEWSY.search(query))


QUESTION_WORDS = {
    "how", "who", "whom", "whose", "when", "where", "why", "which", "does", "do", "did",
    "can", "could", "should", "will", "would", "much", "many", "i", "me", "my", "you",
    "tell", "search", "look", "up", "find", "out", "please", "time",
}  # fmt: skip


def key_words(query: str) -> set[str]:
    found = news.words(query) - QUESTION_WORDS
    return found or news.words(query)


def headline_for(query: str) -> str:
    text = query[:1].upper() + query[1:]
    if re.match(r"(?:what|who|when|where|why|which|how|is|are|does|do|did|can|should|will)\b",
                query, re.IGNORECASE) and not text.endswith("?"):
        text += "?"
    return text[:200]


# --- Cache ------------------------------------------------------------------------------

_CACHE: OrderedDict[str, tuple[float, float, SearchResponse]] = OrderedDict()


def _cached(key: str) -> SearchResponse | None:
    hit = _CACHE.get(key)
    if not hit:
        return None
    stored, ttl, response = hit
    if time.monotonic() - stored >= ttl:
        _CACHE.pop(key, None)
        return None
    return response


def _remember(key: str, response: SearchResponse, ttl: float) -> None:
    _CACHE.pop(key, None)
    _CACHE[key] = (time.monotonic(), ttl, response)
    while len(_CACHE) > CACHE_SIZE:
        _CACHE.popitem(last=False)


# --- Search -----------------------------------------------------------------------------


# When the shared search's engines all come back empty (DuckDuckGo rate-limits bursts),
# a few other keyless engines are raced before giving up.
def _spare_engines() -> tuple[str, ...]:
    """Installed ddgs text engines the shared race doesn't already ask."""
    try:
        from ddgs.engines import ENGINES

        installed = set(ENGINES.get("text", {}))
    except (ImportError, AttributeError):
        installed = set()
    wanted = ("mojeek", "startpage", "wikipedia", "brave", "google")
    return tuple(e for e in wanted if e in installed and e not in RACE)[:3]


SPARE_ENGINES = _spare_engines()
SPARE_SECONDS = 3


def _spare_rows(query: str, backend: str) -> list[dict]:
    from ddgs import DDGS

    return DDGS(timeout=SPARE_SECONDS).text(query, region="us-en", max_results=10, backend=backend)


async def spare_search(query: str) -> list[dict]:
    async def attempt(backend):
        try:
            return await asyncio.wait_for(
                asyncio.to_thread(_spare_rows, query, backend), SPARE_SECONDS + 1
            )
        except Exception:  # noqa: BLE001 -- any engine failure means try another
            return []

    racers = [asyncio.create_task(attempt(b)) for b in SPARE_ENGINES]
    try:
        for finished in asyncio.as_completed(racers):
            rows = await finished
            hits = [h for r in rows or [] if (h := news._hit(r.get("href"), r.get("title"), r.get("body")))]
            if hits:
                return hits
    finally:
        for racer in racers:
            racer.cancel()
    raise SearchUnavailable("No search engine answered")


async def web_hits(query: str) -> list[dict]:
    try:
        sources = await free_search(query)
    except SearchUnavailable:
        report("fallback", "Main search engines were empty; trying others")
        return await spare_search(query)
    return [hit for s in sources if (hit := news._hit(s.url, s.title, s.snippet))]


async def news_hits(query: str) -> list[dict]:
    return await news.ddgs_news(news.parse_query(query), "w")


SEARCH_FAILURES = (SearchUnavailable, aiohttp.ClientError, TimeoutError, ValueError)


def _ok(task: asyncio.Task) -> bool:
    return task.done() and not task.cancelled() and task.exception() is None


async def find_hits(
    query: str, newsy: bool, budget: float, grace: float = 0.0
) -> tuple[list[dict] | None, bool]:
    """(hits, timed_out): hits None when no provider answered. News first for news.

    Every provider gets `budget` seconds. When none has a result by then, the first
    useful answer is still awaited for up to `grace` more seconds."""
    tasks = {"web": asyncio.create_task(web_hits(query))}
    if newsy:
        tasks["news"] = asyncio.create_task(news_hits(query))
    loop = asyncio.get_running_loop()
    hard = loop.time() + max(budget, 0.1) + max(grace, 0.0)
    done, pending = await asyncio.wait(tasks.values(), timeout=max(budget, 0.1))
    while pending and not any(_ok(t) and t.result() for t in done):
        remaining = hard - loop.time()
        if remaining <= 0:
            break
        more, pending = await asyncio.wait(
            pending, timeout=remaining, return_when=asyncio.FIRST_COMPLETED
        )
        done |= more
    for task in pending:
        task.cancel()
    results, answered = {}, False
    for name, task in tasks.items():
        if task in done and not task.cancelled():
            error = task.exception()
            if error is None:
                results[name], answered = task.result(), True
            elif not isinstance(error, SEARCH_FAILURES):
                raise error
    if not answered:
        return None, bool(pending)
    merged, seen = [], set()
    for hit in results.get("news", []) + results.get("web", []):
        if news.canonical(hit["url"]) not in seen:
            seen.add(news.canonical(hit["url"]))
            merged.append(hit)
    return merged, bool(pending)


# --- Reading ----------------------------------------------------------------------------


def snippet_only(hit: dict) -> dict:
    return {**hit, "text": "", "image": None, "image_alt": None,
            "image_width": None, "image_height": None}  # fmt: skip


async def read_pages(hits: list[dict], budget: float) -> tuple[list[dict], bool]:
    """Every hit, read from its page when that page arrived within the budget."""
    tasks = [asyncio.create_task(news.read_article(h)) for h in hits]
    if not tasks:
        return [], False
    done, pending = await asyncio.wait(tasks, timeout=max(budget, 0.05))
    for task in pending:
        task.cancel()
    articles = []
    for hit, task in zip(hits, tasks):
        ok = task in done and not task.cancelled() and task.exception() is None
        articles.append(task.result() if ok else snippet_only(hit))
    return articles, bool(pending)


# --- Ranking ----------------------------------------------------------------------------


def rank(articles: list[dict], query: str, newsy: bool) -> list[dict]:
    """News questions rank like the news agent (recency counts). Otherwise the search
    engine's own order counts, with how well title, snippet and page text match."""
    articles = [a for a in articles if a.get("title")]
    if newsy:
        return news.rank(articles, query)
    qw = key_words(query)
    n = max(len(articles), 1)
    scored = []
    for i, a in enumerate(articles):
        score = 1.0 * (1 - i / n)
        if qw:
            score += 2 * len(qw & news.words(a["title"])) / len(qw)
            score += len(qw & news.words(a.get("lead") or "")) / len(qw)
            score += 0.5 * len(qw & news.words((a.get("text") or "")[:4000])) / len(qw)
        scored.append((score, i, a))
    ranked, urls = [], set()
    for _, _, a in sorted(scored, key=lambda x: (-x[0], x[1])):
        url = news.canonical(a["url"])
        # Different sites about the same thing are separate sources here; only the same
        # page twice is dropped.
        if url not in urls:
            ranked.append(a)
        urls.add(url)
    return ranked


# --- Answer -----------------------------------------------------------------------------

ANSWER_PROMPT = (
    "You answer a question for a heads-up display using only the numbered web sources "
    "given. Answer directly in the first sentence, then add what helps, in 2 to 4 plain "
    "sentences and at most 500 characters. Leave out anything that does not help answer "
    "the question. State only facts that appear in the sources; "
    "do not add background knowledge or guess. Do not write any number, time, date or "
    "amount that is not in the sources. If the sources do not answer the question, say "
    "so in one sentence. No markdown, lists, headings, citations or preamble."
)
LLM_ARTICLES = 4
LLM_CHARS_PER_ARTICLE = 2000


def source_text(articles: list[dict]) -> str:
    blocks = []
    for n, a in enumerate(articles[:LLM_ARTICLES], 1):
        body = " ".join(x for x in (a.get("lead"), a.get("text")) if x)
        blocks.append(
            f"[{n}] {a.get('publisher') or news.publisher_of(a['url'])}: {a['title']}\n"
            f"{body[:LLM_CHARS_PER_ARTICLE]}"
        )
    return "\n\n".join(blocks)


# A measurement: a number and its unit ("30 °F", "86°F", "30 degrees Celsius", "9 mph").
MEASURE = re.compile(
    r"(\d[\d,.]*)\s*(?:°\s*|degrees?\s+)?(f(?:ahrenheit)?|c(?:elsius)?|mph|km/h|kph)\b", re.IGNORECASE,
)


def _unit(token: str) -> str:
    t = token.lower()
    return {"fahrenheit": "f", "celsius": "c", "kph": "km/h"}.get(t, t)


def units_supported(sentence: str, allowed: str) -> bool:
    """Every measurement in the sentence appears in the sources with the same unit:
    a model must not relabel 30 °C as 30 °F."""
    have = {(n.rstrip(".,").replace(",", ""), _unit(u)) for n, u in MEASURE.findall(allowed)}
    return all((n.rstrip(".,").replace(",", ""), _unit(u)) in have for n, u in MEASURE.findall(sentence))


def grounded(answer: str, sources: str, question: str = "") -> str | None:
    """The answer's sentences, minus any that has a number the sources (or the question
    itself) don't contain, or a measurement with a different unit; None when nothing is left."""
    answer = re.sub(r"[*_#`>]+", "", answer or "")
    allowed = f"{sources}\n{question}"
    kept = [
        s for s in news.sentences(answer)
        if not s.startswith(("-", "•")) and news.numbers_supported(s, allowed)
        and units_supported(s, allowed)
    ]
    return news.fit(kept, ANSWER_LIMIT) if kept else None


async def llm_answer(question: str, articles: list[dict], seconds: float) -> str | None:
    if not llm.has_key() or seconds < LLM_MIN_SECONDS:
        return None
    text = source_text(articles)
    try:
        reply = await asyncio.wait_for(
            llm.complete(ANSWER_PROMPT, f"Question: {question}\n\nSources:\n{text}", timeout=seconds),
            seconds,
        )
    except TimeoutError:
        return None
    if not reply:
        return None
    return grounded(reply, text, question)


WANTS_NUMBER = re.compile(
    r"^\s*(?:when|what\s+time|what\s+year|how\s+(?:much|many|long|old|far|tall|big|hot|cold))\b"
    r"|\b(?:hours|open|close|closes|opens|price|cost|population|temperature|score)\b",
    re.IGNORECASE,
)


def candidate_sentences(text: str, whole: bool) -> list[str]:
    """Sentences fit to show. A search snippet (whole=True) may lack a final period."""
    out = []
    for s in news.sentences(text or ""):
        s = news.AGE_PREFIX.sub("", s).strip()
        if not 25 <= len(s) <= 400 or s.endswith(("...", "…")):
            continue
        if news.BOILERPLATE.search(s) or news.CAPTION.search(s):
            continue
        if s[-1] not in ".!?\"”'’)":
            if not whole:
                continue
            s += "."
        out.append(s)
    return out


def extractive_answer(question: str, articles: list[dict]) -> str | None:
    """The sentences from the top results that best match the question, verbatim."""
    qw = key_words(question)
    numeric = bool(WANTS_NUMBER.search(question))
    pool: list[tuple[float, int, str]] = []
    for r, a in enumerate(articles[:5]):
        lead = candidate_sentences(a.get("lead") or "", whole=True)
        body = candidate_sentences((a.get("text") or "")[:20000], whole=False)[:60]
        for pos, s in enumerate(lead + body):
            ws = news.words(s)
            overlap = len(qw & ws) / len(qw) if qw else 0.0
            if qw and not overlap:
                continue
            score = 2 * overlap + 0.4 * (1 - r / 5) + (0.2 if pos < 3 else 0.0)
            if numeric and re.search(r"\d", s):
                score += 0.6
            pool.append((score, len(pool), s))
    picked: list[str] = []
    for _, _, s in sorted(pool, key=lambda x: (-x[0], x[1])):
        if any(len(news.words(s) & news.words(p)) / max(len(news.words(s) | news.words(p)), 1) >= 0.5
               for p in picked):  # fmt: skip
            continue
        picked.append(s)
        if len(picked) >= 3:
            break
    return news.fit(picked, ANSWER_LIMIT) if picked else None


# --- Weather ----------------------------------------------------------------------------
# Only these two fixed Open-Meteo hosts are requested; the place name is a query value.

WEATHER = re.compile(
    r"\b(?:weather|forecast|temperature|how\s+(?:hot|cold|warm)\s+is\s+it|"
    r"(?:is\s+it|will\s+it)\s+(?:going\s+to\s+)?(?:rain|snow))\b",
    re.IGNORECASE,
)
PLACE = re.compile(
    r"\b(?:in|for|at|near|around)\s+(?P<place>[A-Za-z][A-Za-z .,'-]{1,60}?)"
    r"(?:\s+(?:today|tonight|tomorrow|this\s+week(?:end)?|right\s+now|now|currently))*\s*[?.!]*$",
    re.IGNORECASE,
)
PLACE_ALIASES = {
    "la": "Los Angeles", "l.a": "Los Angeles", "ucla": "Los Angeles", "westwood": "Los Angeles",
    "nyc": "New York", "ny": "New York", "sf": "San Francisco", "dc": "Washington",
    "usc": "Los Angeles", "the bay area": "San Francisco",
}  # fmt: skip
WEATHER_CODES = {
    0: "clear", 1: "mostly clear", 2: "partly cloudy", 3: "overcast", 45: "foggy", 48: "foggy",
    51: "light drizzle", 53: "drizzle", 55: "heavy drizzle", 61: "light rain", 63: "rain",
    65: "heavy rain", 66: "freezing rain", 67: "freezing rain", 71: "light snow", 73: "snow",
    75: "heavy snow", 77: "snow grains", 80: "rain showers", 81: "rain showers",
    82: "heavy rain showers", 85: "snow showers", 86: "snow showers", 95: "thunderstorms",
    96: "thunderstorms with hail", 99: "thunderstorms with hail",
}  # fmt: skip
GEOCODE = "https://geocoding-api.open-meteo.com/v1/search"
_PLACES: dict[str, dict] = {}  # place name -> geocoding result; places don't move
FORECAST = "https://api.open-meteo.com/v1/forecast"


def weather_place(query: str) -> str | None:
    if not WEATHER.search(query):
        return None
    match = PLACE.search(query)
    if not match:
        return None
    place = match["place"].strip(" .,'-")
    return PLACE_ALIASES.get(place.lower().rstrip("."), place) or None


async def _get_json(url: str, params: dict, seconds: float) -> dict | None:
    try:
        async with net.session().get(
            url, params=params, timeout=aiohttp.ClientTimeout(total=max(seconds, 0.2)),
            allow_redirects=False,
        ) as response:
            if response.status != 200:
                return None
            data = await response.json(content_type=None)
            return data if isinstance(data, dict) else None
    except (aiohttp.ClientError, TimeoutError, ValueError):
        return None


def _deg(value) -> str | None:
    return f"{round(value)}°F" if isinstance(value, int | float) else None


def weather_summary(place: dict, data: dict, tomorrow: bool) -> str | None:
    """Plain sentences from the forecast's own numbers only."""
    name = ", ".join(x for x in (place.get("name"), place.get("admin1")) if x)
    daily, current = data.get("daily") or {}, data.get("current") or {}
    day = 1 if tomorrow else 0

    def daily_value(key):
        values = daily.get(key) or []
        return values[day] if len(values) > day else None

    parts = []
    if not tomorrow and _deg(current.get("temperature_2m")):
        sky = WEATHER_CODES.get(current.get("weather_code"))
        line = f"{name}: {_deg(current['temperature_2m'])}{' and ' + sky if sky else ''} now"
        if _deg(current.get("apparent_temperature")):
            line += f", feels like {_deg(current['apparent_temperature'])}"
        if isinstance(current.get("wind_speed_10m"), int | float):
            line += f", wind {round(current['wind_speed_10m'])} mph"
        parts.append(line + ".")
    high, low = _deg(daily_value("temperature_2m_max")), _deg(daily_value("temperature_2m_min"))
    if high and low:
        label = "Tomorrow" if tomorrow else "Today"
        sky = WEATHER_CODES.get(daily_value("weather_code"))
        line = f"{label}{' in ' + name if tomorrow else ''}: {sky + ', ' if sky else ''}high {high}, low {low}"
        rain = daily_value("precipitation_probability_max")
        if isinstance(rain, int | float):
            line += f", {round(rain)}% chance of rain"
        parts.append(line + ".")
    return " ".join(parts) or None


WTTR = "https://wttr.in/{place}"


def wttr_summary(place_name: str, data: dict, tomorrow: bool) -> str | None:
    """wttr.in's JSON (format=j1) as one line, like weather_summary."""
    try:
        area = (data.get("nearest_area") or [{}])[0]
        name = ", ".join(x for x in (
            ((area.get("areaName") or [{}])[0].get("value")), ((area.get("region") or [{}])[0].get("value"))
        ) if x) or place_name  # fmt: skip
        days = data.get("weather") or []
        day = days[1 if tomorrow else 0] if len(days) > (1 if tomorrow else 0) else {}
        parts = []
        if not tomorrow:
            now = (data.get("current_condition") or [{}])[0]
            sky = ((now.get("weatherDesc") or [{}])[0].get("value") or "").strip().lower()
            if now.get("temp_F"):
                parts.append(f"{name}: {now['temp_F']}°F{' and ' + sky if sky else ''} now")
        if day.get("maxtempF") and day.get("mintempF"):
            label = "Tomorrow" if tomorrow else "Today"
            parts.append(f"{label}: high {day['maxtempF']}°F, low {day['mintempF']}°F"
                         if parts else f"{name}, {label.lower()}: high {day['maxtempF']}°F, low {day['mintempF']}°F")
        return ". ".join(parts) + "." if parts else None
    except (AttributeError, IndexError, TypeError):
        return None


async def wttr_answer(query: str, place_name: str, budget: float) -> SearchResponse | None:
    """The backup when Open-Meteo doesn't answer (it drops out on some networks)."""
    from urllib.parse import quote

    data = await _get_json(WTTR.format(place=quote(place_name)), {"format": "j1"}, budget)
    summary = wttr_summary(place_name, data or {}, bool(re.search(r"\btomorrow\b", query, re.IGNORECASE)))
    if not summary:
        return None
    headline = f"Weather in {place_name}"
    return SearchResponse(
        query=query, headline=headline, title=headline, summary=summary, data_source="wttr.in",
        sources=[NewsSource(title=f"wttr.in forecast for {place_name}", url="https://wttr.in/",
                            publisher="wttr.in")],
    )  # fmt: skip


async def weather_answer(query: str, place_name: str, budget: float) -> SearchResponse | None:
    until = time.monotonic() + budget
    # Open-Meteo first, with most of the time; wttr.in gets what is left if it fails.
    answer = await _open_meteo_answer(query, place_name, budget * 0.6)
    return answer or await wttr_answer(query, place_name, max(until - time.monotonic(), 1.0))


async def _open_meteo_answer(query: str, place_name: str, budget: float) -> SearchResponse | None:
    until = time.monotonic() + budget
    place = _PLACES.get(place_name.lower())
    if place is None:
        found = await _get_json(GEOCODE, {"name": place_name, "count": 1, "language": "en",
                                          "format": "json"}, min(1.5, budget / 2))  # fmt: skip
        places = (found or {}).get("results") or []
        if not places or not isinstance(places[0], dict):
            return None
        place = places[0]
        if len(_PLACES) < 256:
            _PLACES[place_name.lower()] = place
    lat, lon = place.get("latitude"), place.get("longitude")
    if not isinstance(lat, int | float) or not isinstance(lon, int | float):
        return None
    data = await _get_json(FORECAST, {
        "latitude": lat, "longitude": lon, "timezone": "auto", "forecast_days": 2,
        "temperature_unit": "fahrenheit", "wind_speed_unit": "mph",
        "current": "temperature_2m,apparent_temperature,weather_code,wind_speed_10m",
        "daily": "temperature_2m_max,temperature_2m_min,precipitation_probability_max,weather_code",
    }, until - time.monotonic())  # fmt: skip
    if not data:
        return None
    summary = weather_summary(place, data, bool(re.search(r"\btomorrow\b", query, re.IGNORECASE)))
    if not summary:
        return None
    name = place.get("name") or place_name
    headline = f"Weather in {name}"
    return SearchResponse(
        query=query, headline=headline, title=headline, summary=summary,
        data_source="open-meteo",
        sources=[NewsSource(title=f"Open-Meteo forecast for {name}", url="https://open-meteo.com/",
                            publisher="open-meteo.com")],
    )  # fmt: skip


# --- Agent ------------------------------------------------------------------------------


def chat_reply(result: SearchResponse) -> str:
    lines = [x for x in (result.headline, result.summary) if x]
    lines += [f"{s.publisher}: {s.url}" for s in result.sources[:MAX_SOURCES_OUT]]
    return "\n".join(lines) or "No answer found."


def publisher(article: dict) -> str:
    name = article.get("publisher") or ""
    # Some sites put a URL in og:site_name.
    return news.publisher_of(article["url"]) if not name or "/" in name else name


def _sources(ranked: list[dict]) -> list[NewsSource]:
    return [
        NewsSource(
            title=a["title"][:300],
            url=a["url"],
            publisher=publisher(a),
            published_at=a.get("published_at"),
        )
        for a in ranked[:MAX_SOURCES_OUT]
    ]


@tracked("search")
async def answer(req: SearchRequest) -> SearchResponse:
    started = time.monotonic()
    until = started + DEADLINE

    def left() -> float:
        return until - time.monotonic()

    def done(response: SearchResponse) -> SearchResponse:
        return response.copy(update={"elapsed_ms": round((time.monotonic() - started) * 1000)})

    query = clean_query(req.query)
    newsy = news_like(query)
    key = f"{query.lower()}|{req.max_sources}"
    if (cached := _cached(key)) is not None:
        report("cached", "Answered from a recent identical search")
        return done(cached)

    place = weather_place(query)
    report("searching", f"Searching the web for {query}")
    # Past its share, search may run on while nothing has come back: snippets alone can
    # still be answered from (extractively) in the last half second.
    search = asyncio.create_task(
        find_hits(query, newsy, DEADLINE * SEARCH_SHARE, DEADLINE * (1 - SEARCH_SHARE) - 0.5)
    )
    if place:
        # The forecast API sometimes takes over 2 s; the web search runs meanwhile.
        forecast = await weather_answer(query, place, DEADLINE - 0.5)
        if forecast is not None:
            # The forecast is the answer. Web results already in (or within a moment)
            # only add sources after it; a slow search is not waited for.
            await asyncio.wait({search}, timeout=0.3)
            hits = search.result()[0] if search.done() and not search.exception() else None
            if not search.done():
                search.cancel()
            extra = rank(list(map(snippet_only, hits or [])), query, False)
            response = forecast.copy(update={
                "sources": (forecast.sources + _sources(extra))[:MAX_SOURCES_OUT],
            })  # fmt: skip
            _remember(key, response, FRESH_CACHE_SECONDS)
            return done(response)
    hits, slow = await search
    if hits is None:
        error = "Search timed out" if slow else "Search unavailable"
        return done(SearchResponse(
            query=query, error=error, partial=slow, news=newsy,
            summary="Web search didn't answer in time; nothing was made up." if slow
            else "Web search is unavailable right now; nothing was made up.",
        ))  # fmt: skip
    if not hits:
        return done(SearchResponse(query=query, news=newsy, headline=headline_for(query),
                                   title=headline_for(query),
                                   summary=f"No web results found for {query}."))  # fmt: skip

    picked = hits[: req.max_sources]
    # Leave time to answer: about a second for the summary, less without an LLM.
    reserve = 1.6 if llm.has_key() else 0.3
    report("reading", f"Reading {len(picked)} pages")
    articles, cut = await read_pages(picked, min(READ_SECONDS, left() - reserve))
    ranked = rank(articles, query, newsy)
    if not ranked:
        return done(SearchResponse(query=query, news=newsy, partial=slow or cut,
                                   summary=f"No web results found for {query}."))  # fmt: skip
    report("summarizing", f"Answering from {min(len(ranked), LLM_ARTICLES)} pages")
    summary = await llm_answer(query, ranked, left() - 0.1)
    if summary is None and llm.has_key() and left() < LLM_MIN_SECONDS + 0.1:
        cut = True  # the deadline left no time for the LLM
    summary = summary or extractive_answer(query, ranked)
    if summary is None:
        summary = f"No clear answer in the results. Top result: {ranked[0]['title']}."
    headline = ranked[0]["title"][:200] if newsy else headline_for(query)
    image = news.pick_image(ranked)
    response = SearchResponse(
        query=query,
        headline=headline,
        title=headline,
        summary=summary,
        image_url=image[0] if image else None,
        image_alt=image[1] if image else None,
        image_source_url=image[2] if image else None,
        sources=_sources(ranked),
        partial=slow or cut,
        news=newsy,
        data_source="web+news" if newsy else "web",
    )
    # A partial answer is kept only briefly: the next ask may read more in time.
    ttl = FRESH_CACHE_SECONDS if newsy else CACHE_SECONDS
    _remember(key, response, PARTIAL_CACHE_SECONDS if response.partial else ttl)
    return done(response)
