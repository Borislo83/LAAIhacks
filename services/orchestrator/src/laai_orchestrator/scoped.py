"""Chats scoped to one agent, for the dashboard's per-agent view.

Each specialist chat calls only its own agent's route; nothing here can reach another
agent. The coordinator (focal-shopping-assistant) is the exception: its chat is the full
orchestrated chat. Cart and iMessage side effects keep their existing approval paths:
the buyer chat confirms only a cart review token the user approved, and the messenger
chat only drafts (sending is POST /v1/messages/send).
"""

import re
import time
from enum import StrEnum
from urllib.parse import urlsplit
from uuid import UUID

import httpx

from .chat import (
    awaiting_recipient,
    buyer_call,
    call_agent,
    chat,
    is_confirmation,
    normalize,
    prepare_message,
    rules_reply,
)
from .config import Config
from .models import AgentCall, AgentKind, ChatMessage, ChatReply, ToolArgs


class AgentName(StrEnum):
    COORDINATOR = "focal-shopping-assistant"
    PRICE = "laai-price-compare"
    ALTERNATIVES = "laai-alternatives"
    REVIEWS = "laai-reviews"
    VERIFIER = "focal-price-verifier"
    MEMORY = "focal-memory"
    BUYER = "focal-amazon-buyer-demo"
    MESSENGER = "laai-messenger"
    YOUTUBE = "laai-youtube"
    NEWS = "laai-news"


SPECIALISTS = {
    AgentName.PRICE: AgentKind.PRICE,
    AgentName.ALTERNATIVES: AgentKind.ALTERNATIVES,
    AgentName.REVIEWS: AgentKind.REVIEWS,
    AgentName.YOUTUBE: AgentKind.YOUTUBE,
    AgentName.NEWS: AgentKind.NEWS,
}
BUDGET = re.compile(
    r"\b(?:under|below|less than|up to|at most|budget(?: of)?|max(?:imum)?(?: of)?)\s*\$?\s*"
    r"(\d[\d,]*(?:\.\d+)?)",
    re.IGNORECASE,
)
# "Amazon $279.99", "Best Buy: 299", "dealz4u.shop - $99 https://dealz4u.shop/x"
OFFER = re.compile(
    r"(?P<store>[A-Za-z][\w.&' -]{0,40}?)\s*[:=–-]?\s*\$\s*(?P<price>\d[\d,]*(?:\.\d{1,2})?)"
    r"(?:\s+(?P<url>https://\S+))?"
)
# The price agent's trusted stores, matched by name since pasted offers have no page.
TRUSTED_NAMES = {
    "amazon",
    "apple",
    "best buy",
    "bestbuy",
    "walmart",
    "target",
    "costco",
    "newegg",
    "b&h",
    "bh photo",
    "adorama",
    "crutchfield",
    "samsung",
    "sony",
    "bose",
    "soundcore",
    "nothing",
}
VERIFIER_HELP = (
    "Paste the offers you want checked, one store and price each, for example: "
    '"Sony XM5: Amazon $279, Best Buy $299, Walmart $310, dealz4u $99". '
    "I only audit what you paste; this chat never asks the price agent for offers."
)
REMEMBER = re.compile(
    r"^(?:please\s+)?remember(?:\s+that)?\s*[:,-]?\s+(?P<note>.+)", re.IGNORECASE | re.DOTALL
)


def budget(text: str) -> float | None:
    found = BUDGET.search(text)
    return float(found[1].replace(",", "")) if found else None


def media_kind(text: str) -> str:
    if re.search(r"video", text, re.IGNORECASE):
        return "video"
    return "song" if re.search(r"\bsongs?\b|\bmusic\b", text, re.IGNORECASE) else "auto"


def pasted_offers(text: str) -> tuple[str, list[dict]]:
    """Product and offers from a pasted list. Store names decide trust; nothing is fetched."""
    head, sep, rest = text.partition(":")
    product = head.strip() if sep and not OFFER.search(head) else "the pasted offers"
    body = rest if product != "the pasted offers" else text
    offers = []
    for part in re.split(r"[,;\n]", body):
        found = OFFER.search(part)
        # "max $300" / "under $250" is the budget, not an offer.
        if not found or BUDGET.search(part):
            continue
        store = re.sub(r"^(?:and|or)\s+", "", found["store"].strip(" -:"), flags=re.IGNORECASE)
        url = found["url"] or ""
        if url and urlsplit(url).scheme != "https":
            url = ""
        known = any(store.lower().startswith(name) for name in TRUSTED_NAMES)
        offers.append(
            {
                "product": product,
                "store": store[:80],
                "price": float(found["price"].replace(",", "")),
                "url": url,
                # Trusted store but no page evidence: medium at best, never high.
                "source": "user_pasted",
                "trust": "medium" if known else "low",
            }
        )
    return product[:200], offers[:20]


async def direct(route, kind, payload, config, http, emit, timeout=60) -> AgentCall:
    """One agent call without the specialist retry loop, with start and finish progress."""
    started = time.perf_counter()
    if emit:
        emit({"type": "progress", "kind": kind, "stage": "delegated", "message": "Sending request"})
    result, error = None, f"{kind.capitalize()} agent unavailable or returned an invalid response"
    try:
        # Only a fixed route string is fetched; nothing from chat text selects a URL.
        response = await http.post(
            str(config.agents_url).rstrip("/") + route, json=payload, timeout=timeout
        )
        response.raise_for_status()
        answer = response.json()
        if not isinstance(answer, dict) or not isinstance(answer.get("summary"), str):
            raise TypeError("Invalid agent result")
        result, error = answer, None
    except (httpx.HTTPError, ValueError, TypeError):
        pass
    ms = round((time.perf_counter() - started) * 1000)
    if emit:
        emit(
            {
                "type": "progress",
                "kind": kind,
                "stage": "failed" if error else "completed",
                "message": error or "Done",
                "result": result,
                "ms": ms,
            }
        )
    return AgentCall(kind=kind, payload=payload, result=result, error=error, ms=ms)


def reply(call: AgentCall) -> ChatReply:
    text = call.result["summary"] if call.result else call.error or "No answer"
    return ChatReply(reply=text, calls=[call], source="agent")


async def scoped_chat(
    agent: AgentName,
    history: list[ChatMessage],
    config: Config,
    http: httpx.AsyncClient,
    emit=None,
    *,
    user: UUID | None = None,
    purchase_confirmation: str | None = None,
    preferences=None,
) -> ChatReply:
    text = history[-1].content
    if agent == AgentName.COORDINATOR:
        return await chat(history, config, http, emit, purchase_confirmation, preferences)
    if agent in SPECIALISTS:
        kind = SPECIALISTS[agent]
        if kind == AgentKind.YOUTUBE:
            args = ToolArgs(query=text[:300], kind=media_kind(text))
        elif kind == AgentKind.NEWS:
            args = ToolArgs(query=text[:300])
        elif kind == AgentKind.ALTERNATIVES:
            args = ToolArgs(product=text[:200], max_price=budget(text))
        else:
            args = ToolArgs(product=text[:200])
        call = await call_agent(kind, args, config, http, emit)
        return ChatReply(reply=rules_reply([call]).reply, calls=[call], source="agent")
    if agent == AgentName.MESSENGER:
        # Drafts only. A draft is sent only by the user's Approve click (/v1/messages/send).
        name = awaiting_recipient(history)
        call = await prepare_message(f"text {name} {text}" if name else text, config, http, emit)
        return ChatReply(reply=rules_reply([call]).reply, calls=[call], source="agent")
    if agent == AgentName.BUYER:
        # Cart only, never checkout. Confirming needs the review token the user just approved.
        confirming = is_confirmation(normalize(text), purchase_confirmation)
        if confirming and not purchase_confirmation:
            return ChatReply(
                reply="Choose an Amazon listing and review the cart addition first. Nothing was added.",
                calls=[],
                source="agent",
            )
        amazon_url = re.search(r"https://(?:www\.)?amazon\.com/[^\s]+", text, re.IGNORECASE)
        quantity = re.search(r"\b(?:quantity|qty)\s*:?\s*(\d+)\b", text, re.IGNORECASE)
        payload = {
            "product": text[:500],
            "url": amazon_url[0].rstrip(".,)") if amazon_url else None,
            "quantity": min(int(quantity[1]), 20) if quantity else 1,
        }
        call = await buyer_call(
            payload, config, http, emit, purchase_confirmation if confirming else None
        )
        return reply(call)
    if agent == AgentName.VERIFIER:
        product, offers = pasted_offers(text)
        if not offers:
            return ChatReply(reply=VERIFIER_HELP, calls=[], source="agent")
        price = {
            "product": product,
            "offers": offers,
            "summary": f"{len(offers)} offer(s) pasted in chat; not fetched from store pages.",
        }
        payload = {"price": price, "max_price": budget(text)}
        return reply(await direct("/verify", "verifier", payload, config, http, emit))
    if agent == AgentName.MEMORY:
        # Keyed by the signed-in user; the agent stores only a hash of this key.
        key = f"dashboard:{user}" if user else "dashboard:anonymous"
        note = REMEMBER.match(text.strip())
        payload = {"user": key, "remember": {"note": note["note"][:500]} if note else None}
        return reply(await direct("/memory", "memory", payload, config, http, emit, timeout=20))
    raise ValueError(f"Unknown agent {agent}")
