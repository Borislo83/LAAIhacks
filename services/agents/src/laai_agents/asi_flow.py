"""The coordinator's ASI:One conversation: intent to action, narrated step by step.

    memory → brief (form card) → plan → specialists in parallel, streamed live →
    verifier audit (with recovery) → comparison card → buyer (listing + cart review) →
    messenger (draft review) → paid price watch (Payment Protocol, Stripe test mode)

Every agent hop is shown in the chat as it happens. Real side effects (Amazon cart,
iMessage) run only for senders in FOCAL_OWNER_ADDRESSES and only after a card approval;
everyone else gets the same flow with those two steps clearly marked as simulated.
"""

import asyncio
import os
import re
import time
from datetime import UTC, datetime
from uuid import uuid4

from uagents import Context, Protocol
from uagents_core.contrib.protocols.chat import (
    ChatAcknowledgement,
    ChatMessage,
    TextContent,
    chat_protocol_spec,
)
from uagents_core.contrib.protocols.payment import (
    CommitPayment,
    CompletePayment,
    RejectPayment,
    payment_protocol_spec,
)

from . import cards, payments
from .buyer import BuyRequest, BuyResponse, CartApproval
from .cards import card, code_block, money, parse_reply, selection
from .imessage import (
    MessageApproval,
    MessageDraftRequest,
    MessageDraftResponse,
    MessageSendResponse,
)
from .imessage import dry_run as imessage_dry_run
from .memory import MemoryRequest, MemoryResponse
from .models import (
    CartLink,
    CartRequest,
    ConsiderRequest,
    NewsRequest,
    NewsResponse,
    PriceCompareRequest,
    PriceCompareResponse,
    YouTubeRequest,
    YouTubeResponse,
)
from .news import chat_reply
from .progress import read
from .verifier import VerifyRequest, VerifyResponse

MEMORY = "focal-memory"
BUYER = "focal-amazon-buyer-demo"
MESSENGER = "laai-messenger"
PRICE = "laai-price-compare"
VERIFIER = "focal-price-verifier"

AGENTS = {
    "memory": ("🧠", "Memory agent", MEMORY),
    "price": ("💲", "Price agent", PRICE),
    "price_retry": ("💲", "Price agent (deep search)", PRICE),
    "reviews": ("⭐", "Reviews agent", "laai-reviews"),
    "alternatives": ("🔀", "Alternatives agent", "laai-alternatives"),
    "alternative_reviews": ("⭐", "Reviews agent (rival)", "laai-reviews"),
    "verification": ("🛡️", "Verifier agent", VERIFIER),
    "buyer": ("🛒", "Buyer agent", BUYER),
    "cart_link": ("🔗", "Amazon cart-link agent", "laai-amazon-cart"),
    "messenger": ("💬", "Messenger agent", MESSENGER),
    "watch": ("👀", "Watch (coordinator)", None),
    "news": ("📰", "News agent", "laai-news"),
    "stripe": ("💳", "Stripe", None),
    "youtube": ("▶️", "YouTube agent", "laai-youtube"),
}

# FOCAL routes each request to the agents that own it; shopping is the deepest flow.
ROUTES = (
    ("message", re.compile(r"^\s*(?:please\s+)?(?:text|message|imessage|tell)\s+\w+|\bsend (?:a |an )?(?:message|text|imessage)\b", re.IGNORECASE)),
    ("youtube", re.compile(r"\b(?:youtube|song|music|video|play|listen to|watch)\b", re.IGNORECASE)),
    ("news", re.compile(r"\b(?:news|latest on|what'?s happening|headlines?|update me)\b", re.IGNORECASE)),
)


CHUNK_GAP = float(os.getenv("FOCAL_CHUNK_GAP", "1.0"))


def route_of(body: str) -> str:
    return next((name for name, pattern in ROUTES if pattern.search(body)), "shopping")


def owners() -> set[str]:
    return {a.strip() for a in os.getenv("FOCAL_OWNER_ADDRESSES", "").split(",") if a.strip()}


def short(address: str | None) -> str:
    return f"`{address[:10]}…{address[-4:]}`" if address else ""


def secs(ms: int) -> str:
    return f"{ms / 1000:.1f}s"


def message_text(msg: ChatMessage) -> str:
    return " ".join(i.text for i in msg.content if isinstance(i, TextContent)).strip()


class Conversation:
    """One shopper's state, kept in the coordinator's storage between messages."""

    def __init__(self, ctx: Context, sender: str, addresses: dict[str, str]):
        self.ctx, self.sender, self.addresses = ctx, sender, addresses
        self.key = f"focal:{sender}"
        self.state = ctx.storage.get(self.key) or {}
        # ASI:One shows only an agent's latest message per turn, so every update carries
        # the whole running log: the bubble grows as each agent reports.
        self.log: list[str] = []  # everything said this turn (tests and the recap read it)
        self.pending: list[str] = []  # notes not yet sent; the next message carries them
        self.last_send = 0.0
        # Parallel agents report at the same instant; ASI:One drops chunks that land
        # together, so every send goes through one queue with a small gap.
        self.sending = asyncio.Lock()

    @property
    def shopper(self) -> str:
        """Memory belongs to the Focal Studio profile when one is configured: ASI:One may
        relay messages through its own agent, so the sender address is not stable."""
        return os.getenv("FOCAL_MEMORY_USER_ID") or self.sender

    def save(self):
        self.ctx.storage.set(self.key, self.state)
        self.ctx.storage.set("focal:last", self.key)

    def reset(self):
        self.state = {"team": []}
        self.save()

    def note(self, body: str):
        """Queue a line without sending it: the next message or card carries it."""
        self.log.append(body)
        self.pending.append(body)

    async def pace(self):
        """Cards and payment requests wait a beat after the previous chunk."""
        wait = 1.2 - (time.monotonic() - self.last_send)
        if wait > 0:
            await asyncio.sleep(wait)

    async def send(self, msg):
        async with self.sending:
            wait = CHUNK_GAP - (time.monotonic() - self.last_send)
            if wait > 0:
                await asyncio.sleep(wait)
            await self.ctx.send(self.sender, msg)
            self.last_send = time.monotonic()

    async def end_stream(self):
        """Kept for callers; FOCAL sends whole messages, not streams (see say)."""

    async def say(self, body: str, end: bool = False):
        """Send the turn's whole conversation so far. ASI:One keeps an agent's latest
        message per turn, so each update carries every line said before it; the send
        queue spaces messages so none of them land together."""
        self.log.append(body)
        self.pending = []
        await self.send(cards.text("\n\n".join(self.log), end_session=end))

    async def show(self, msg: ChatMessage):
        """Send a card whose narration is the whole conversation so far."""
        for item in msg.content:
            if isinstance(item, TextContent):
                self.log.append(item.text)
                item.text = "\n\n".join(self.log)
        self.pending = []
        await self.pace()
        await self.send(msg)

    def credit(self, field: str, did: str, ms: int | None = None):
        self.state.setdefault("team", []).append([field, did, ms])

    def who(self, field: str) -> str:
        if field == "focal":
            return "🎯 **FOCAL**"
        if field == "you":
            return "👤 **you**"
        emoji, name, _ = AGENTS[field]
        return f"{emoji} **{name.split(' (')[0]}**"

    def said(self, src: str, dst: str, text: str, ms: int | None = None) -> str:
        """One line of agent-to-agent conversation: sender → recipient: message."""
        took = f" _({secs(ms)})_" if ms is not None else ""
        return f"{self.who(src)} → {self.who(dst)}: {text}{took}"

    async def talk(self, src: str, dst: str, text: str, ms: int | None = None):
        await self.say(self.said(src, dst, text, ms))

    def line(self, field: str) -> str:
        emoji, name, agent = AGENTS[field]
        return f"{emoji} **{name}** {short(self.addresses.get(agent or ''))}"

    async def ask(self, field: str, message, response_type, timeout: int = 45):
        address = self.addresses.get(AGENTS[field][2])
        if not address:
            return None
        try:
            reply, _ = await self.ctx.send_and_receive(address, message, response_type, timeout=timeout)
        except Exception:  # noqa: BLE001 -- a missing agent must not end the conversation
            return None
        return reply if isinstance(reply, response_type) else None

    # ---- entry -------------------------------------------------------------------

    async def handle(self, body: str):
        reply = parse_reply(body)
        action = reply.get("focal_action")
        # ASI:One's payment card answers "Confirm Payment" with this token in chat.
        if confirm := re.search(r"stripe:payment_id:([\w-]+):CONFIRM", body, re.IGNORECASE):
            action, reply["asi_payment_id"] = "focal_payment_confirm", confirm[1]
        # Answer typed messages at once so the chat never sits blank. Card clicks skip
        # it: the card already shows "Submitted" and the first agent line follows.
        if not action:
            await self.say("⚡ Got it! Thinking it through…")
        if action and self.state.get("stage") in {None, "done"}:
            # ASI:One sometimes relays a card click from its own agent, with a new sender
            # and session: continue the conversation that is waiting for a click.
            last = self.ctx.storage.get("focal:last")
            if last and last != self.key and (self.ctx.storage.get(last) or {}).get("stage") not in {None, "done"}:
                self.key, self.state = last, self.ctx.storage.get(last)
        stage = self.state.get("stage")
        # Only the form itself or a short budget-only reply answers the brief; anything
        # else (a new product question) starts over.
        if stage == "brief" and (
            action == "focal_submit_brief" or (len(body) <= 40 and cards.budget_in(body))
        ):
            if not reply.get("budget"):
                reply["budget"] = cards.budget_in(body)
            return await self.on_brief(reply, body)
        if action in {"focal_buy_main", "focal_buy_alt"} and stage == "decide":
            return await self.on_buy(action == "focal_buy_alt")
        if action == "focal_pick_listing" or (stage == "listing" and reply.get("url")):
            return await self.on_listing(reply)
        if stage == "cart" and action in {"focal_approve_cart", "focal_reject_cart"}:
            return await self.on_cart(action == "focal_approve_cart")
        if stage == "message" and action in {"focal_send_message", "focal_skip_message"}:
            return await self.on_message(action == "focal_send_message")
        if stage == "checkout" and action in {"focal_checkout", "focal_skip_checkout"}:
            return await (self.on_checkout() if action == "focal_checkout" else self.offer_watch())
        if stage == "wrapup" and (action == "focal_wrapup" or (
            action is None and len(body) < 80
            and re.search(r"\b(watch|alert|updat|message|text|tell|memory|remember|save)", body, re.IGNORECASE)
        )):
            return await self.on_wrapup(reply, body)
        if action == "focal_finish":
            return await self.finish()
        if stage == "awaiting_payment" and action == "focal_payment_confirm":
            return await self.check_payment(asi_payment_id=reply["asi_payment_id"])
        if stage == "awaiting_payment" and re.search(r"\b(paid|confirm(?:ed)?|done|payment|cs_test_\w+)", body, re.IGNORECASE):
            return await self.check_payment()
        active = stage not in {None, "done", "researching"}
        if active and re.match(r"\s*(yes|yep|yeah|sure|ok(ay)?|approve|confirm|go ahead|do it|start|send)\b", body, re.IGNORECASE):
            return await self.on_yes(stage)
        if active and re.match(r"\s*(no|nope|skip|cancel|stop|don'?t|not now)\b", body, re.IGNORECASE):
            return await self.on_no(stage)
        return await self.start(body)

    async def on_yes(self, stage: str):
        routes = {
            "decide": lambda: self.on_buy(False),
            "cart": lambda: self.on_cart(True),
            "checkout": self.on_checkout,
            "awaiting_payment": self.check_payment,
            "message": lambda: self.on_message(True),
            "wrapup": lambda: self.on_wrapup({"watch": True, "remember": True}, ""),
        }
        return await routes.get(stage, self.finish)()

    async def on_no(self, stage: str):
        routes = {
            "cart": lambda: self.on_cart(False),
            "checkout": self.offer_watch,
            "message": lambda: self.on_message(False),
        }
        return await routes.get(stage, self.finish)()

    # ---- 1. memory + brief -------------------------------------------------------

    async def recall(self) -> str:
        """Ask the memory agent for the shopper's profile; show the API call it made."""
        await self.talk("focal", "memory", "what do you know about this person?")
        started = time.perf_counter()
        memory = await self.ask("memory", MemoryRequest(user=self.shopper), MemoryResponse, timeout=15)
        ms = round((time.perf_counter() - started) * 1000)
        if not memory:
            await self.talk("focal", "you", "the memory agent didn't answer, so I'll use default preferences.")
            return "balanced"
        self.credit("memory", memory.summary, ms)
        status = f"HTTP {memory.status}" if memory.status else "local"
        await self.say(
            self.said("memory", "focal", f"I called `{memory.request}` → {status}. {memory.summary}", ms)
            + "\n" + code_block(memory.response)
        )
        return memory.preferences.get("shopping_priority", "balanced")

    async def remember(self, entry: dict):
        self.note(self.said("focal", "memory", "remember this for next time."))
        await self.ask("memory", MemoryRequest(user=self.shopper, remember=entry), MemoryResponse, timeout=10)
        self.note(self.said("memory", "focal", "saved to the profile."))

    async def start(self, body: str):
        route = route_of(body)
        if route != "shopping":
            return await getattr(self, f"run_{route}")(body)
        from .coordinator import budget_from, parse_request

        req = await parse_request(body)
        if req is not None and re.search(r"\bno product\b|not (?:provided|specified|mentioned)", req.product, re.IGNORECASE):
            req = None
        if req is None:
            return await self.say(
                "I'm **FOCAL**, a team of 12 agents that turns what you ask into something done. Try:\n"
                "- *\"Should I buy the Apple AirPods Pro 2?\"* — research, verify, cart, tell a friend, watch the price\n"
                "- *\"What's the latest on SpaceX Starship?\"* — a sourced news brief\n"
                "- *\"Play Blinding Lights on YouTube\"* — the right video link\n"
                "- *\"Text Alex I'm running late\"* — a draft you approve before it's sent"
            )
        self.reset()
        self.state.update(product=req.product, budget=req.max_price or budget_from(body))
        await self.talk(
            "focal",
            "you",
            f"you want to decide on **{req.product}**"
            + (f" under {money(self.state['budget'])}" if self.state["budget"] else "")
            + ". Let me check with the team.",
        )
        self.state["priority"] = await self.recall()
        if self.state["budget"] is None:
            self.state["stage"] = "brief"
            self.save()
            return await self.show(cards.brief_form(req.product, self.state["priority"]))
        self.save()
        await self.research()

    # ---- other routes: news, YouTube, messages -------------------------------------

    async def run_news(self, body: str):
        self.reset()
        await self.talk("focal", "you", f"you want the latest on *{body[:120]}*. On it.")
        await self.recall()
        await self.talk("focal", "news", f"read recent coverage of *{body[:120]}* and brief me, with sources.")
        started = time.perf_counter()
        result = await self.ask("news", NewsRequest(query=body[:300]), NewsResponse, timeout=120)
        ms = round((time.perf_counter() - started) * 1000)
        if result is None or result.error:
            self.credit("news", result.error if result else "no answer", ms)
            await self.talk("news", "focal", f"⚠️ {result.error if result else 'no answer'}. Nothing was made up.", ms)
        else:
            self.credit("news", f"{len(result.sources)} sources", ms)
            sources = "\n".join(f"- [{s.publisher}]({s.url})" for s in result.sources[:4])
            await self.say(
                self.said("news", "focal", f"read {len(result.sources)} articles. Here's the brief:", ms)
                + f"\n\n**{result.headline or 'News brief'}**\n\n"
                + chat_reply(result).split("\n", 1)[-1].split("\nhttp")[0]
                + (f"\n\n**Sources**\n{sources}" if sources else "")
            )
            await self.remember({"topic": body[:120], "kind": "news"})
        await self.finish()

    async def run_youtube(self, body: str):
        self.reset()
        await self.talk("focal", "you", f"you want a video: *{body[:120]}*. On it.")
        await self.recall()
        await self.talk("focal", "youtube", f"find the best single video for *{body[:120]}*.")
        started = time.perf_counter()
        result = await self.ask("youtube", YouTubeRequest(query=body[:300]), YouTubeResponse, timeout=60)
        ms = round((time.perf_counter() - started) * 1000)
        if result is None or not result.url:
            self.credit("youtube", result.error if result and result.error else "no match", ms)
            await self.talk("youtube", "focal", f"⚠️ {result.summary if result else 'no answer'}", ms)
        else:
            self.credit("youtube", result.title or "video", ms)
            more = "\n".join(f"- [{v.title}]({v.url})" for v in result.alternatives)
            await self.say(
                self.said("youtube", "focal", "found it:", ms)
                + f"\n▶️ **{result.title}**"
                + (f" · {result.channel}" if result.channel else "")
                + f"\n{result.url}"
                + (f"\n\n**Also**\n{more}" if more else "")
            )
            await self.remember({"topic": result.title, "kind": "youtube"})
        await self.finish()

    async def run_message(self, body: str):
        self.reset()
        self.state["standalone"] = True
        await self.talk("focal", "you", "you want to message a friend. I'll have it drafted for your approval.")
        await self.recall()
        await self.talk("focal", "messenger", f"draft this for me: *{body[:160]}*")
        started = time.perf_counter()
        draft = await self.ask("messenger", MessageDraftRequest(request=body[:1200]), MessageDraftResponse, timeout=30)
        await self.show_draft(draft, round((time.perf_counter() - started) * 1000))

    async def on_brief(self, reply: dict, body: str):
        budget = reply.get("budget")
        try:
            self.state["budget"] = float(str(budget).replace(",", "").replace("$", "")) if budget else None
        except ValueError:
            self.state["budget"] = None
        priority = reply.get("priority") or reply.get("default_priority") or self.state.get("priority")
        self.state["priority"] = priority if priority in cards.PRIORITIES else "balanced"
        self.state["use_case"] = str(reply.get("use_case") or "")[:200]
        self.save()
        await self.research()

    # ---- 2. plan + live orchestration --------------------------------------------

    async def research(self):
        from .coordinator import consider

        product, budget = self.state["product"], self.state.get("budget")
        self.state["stage"] = "researching"
        self.save()
        await self.say(
            self.said("focal", "you", f"here's the plan for **{product}**"
                      + (f" (budget {money(budget)}, priority {self.state.get('priority', 'balanced')})" if budget else "")
                      + ". The team:")
            + f"\n- {self.line('price')} · {self.line('reviews')} · {self.line('alternatives')} work in parallel"
            + f"\n- {self.line('verification')} audits the prices before anyone acts"
            + "\n- then you decide → 🛒 Buyer → 💬 Messenger → 👀 Price watch, each only after you approve"
        )
        progress_id = uuid4().hex
        stop = asyncio.Event()
        forwarder = asyncio.create_task(self.forward_progress(progress_id, stop))
        try:
            result = await consider(
                self.ctx,
                self.addresses,
                ConsiderRequest(product=product, max_price=budget, progress_id=progress_id),
                on_update=self.on_update,
                on_dispatch=self.on_dispatch,
            )
        finally:
            stop.set()
            await forwarder
        await self.present(result)

    def request_for(self, field: str) -> str | None:
        product, budget = self.state.get("product", "it"), self.state.get("budget")
        return {
            "price": f"find current prices for **{product}** across stores; read them off the product pages.",
            "reviews": f"what do the experts say about **{product}**?",
            "alternatives": f"find rivals to **{product}**" + (f" under {money(budget)}" if budget else "") + ", and price-check them.",
            "alternative_reviews": "you found a top rival; I need expert reviews of it too.",
            "verification": "audit these prices before anyone acts on them.",
        }.get(field)

    async def on_dispatch(self, field: str):
        """Called just before a request goes out, so the ask always precedes the answer."""
        if ask := self.request_for(field):
            await self.talk("focal", field, ask)

    async def forward_progress(self, progress_id: str, stop: asyncio.Event):
        """Turn the agents' live progress into conversation lines as it happens."""
        after, said = 0, set()
        while True:
            for event in read(progress_id, after):
                after = event["seq"]
                field, stage = event["kind"], event["stage"]
                if field not in AGENTS or ((field, stage) in said and stage != "fallback"):
                    continue
                said.add((field, stage))
                if stage == "sources_found":
                    await self.talk(field, "focal", f"{event['message'].lower()}, reading them now…")
                elif stage == "fallback":
                    await self.talk(field, "focal", "ASI web search is down, so I switched to the free search fallback.")
                elif stage == "retrying":
                    await self.talk("focal", field, f"no answer yet, asking again ({event['message'].split('(')[-1].rstrip(')')}).")
            if stop.is_set():
                return
            await asyncio.sleep(0.3)

    async def on_update(self, field: str, result, ms: int):
        if field not in AGENTS:
            return
        if result is None:
            self.credit(field, "no answer", ms)
            return await self.talk("focal", "you", f"{self.who(field)} didn't answer after 3 tries, so I'm going on without it. Nothing was made up.")
        if getattr(result, "error", None):
            self.credit(field, result.error, ms)
            return await self.talk(field, "focal", f"⚠️ {result.error}. I won't guess.", ms)
        if field in {"price", "price_retry"}:
            stores = len({o.store for o in result.offers})
            did = f"{len(result.offers)} offers from {stores} stores"
            best = f", best {money(result.best.price)} at {result.best.store} ({result.best.trust} trust)" if result.best else ""
            body = f"{did}{best} · source `{result.data_source}`"
        elif field in {"reviews", "alternative_reviews"}:
            did = (
                f"{result.product}: {result.rating}/5 from {result.review_count} expert reviews"
                if result.rating is not None
                else f"{result.product}: {len(result.sources)} review sources, no rating"
            )
            body = did + (f"\n+ {result.pros[0]}" if result.pros else "") + (f"\n− {result.cons[0]}" if result.cons else "")
        elif field == "alternatives":
            names = ", ".join(a.product for a in result.alternatives[:3]) or "none found"
            did = f"{len(result.alternatives)} alternatives"
            body = f"{did}: {names}"
        elif field == "verification":
            did = f"kept {len(result.kept)}, dropped {len(result.dropped)} · {result.verdict}"
            dropped = "\n".join(f"✗ {d.store} {money(d.price)}: {d.reason}" for d in result.dropped[:4])
            flags = "\n".join(f"⚑ {f}" for f in result.flags)
            body = result.summary + (f"\n{dropped}" if dropped else "") + (f"\n{flags}" if flags else "")
        else:
            did, body = "done", getattr(result, "summary", "")
        self.credit(field, did, ms)
        await self.talk(field, "focal", body, ms)
        if field == "verification" and result.needs_retry:
            await self.talk("verification", "price", "none of these are trustworthy. Search the stores again, deeper.")

    # ---- 3. decision card ----------------------------------------------------------

    async def present(self, result):
        price, reviews, alts = result.price, result.reviews, result.alternatives
        audit = result.verification or {}
        best = audit.get("best") or (price.best.model_dump() if price and price.best else None)
        alt = alts.alternatives[0] if alts and alts.alternatives else None
        alt_reviews = result.alternative_reviews
        self.state.update(
            stage="decide",
            best=best,
            alt={"product": alt.product, "price": alt.price, "url": alt.url} if alt else None,
            amazon_leads=self.amazon_leads(result),
        )
        self.save()

        def column(title, price_value, store, rating, note, badge):
            children = [
                {"type": "heading", "value": title, "level": 3},
                {"type": "badge", "label": badge[0], "variant": badge[1]},
                {"type": "text", "value": f"Price: {money(price_value)}" + (f" at {store}" if store else ""), "style": "emphasis"},
                {"type": "text", "value": f"Rating: {rating}/5" if rating is not None else "Rating: n/a"},
            ]
            if note:
                children.append({"type": "text", "value": note, "style": "muted"})
            return {"type": "group", "direction": "column", "gap": 6, "children": children}

        verdict = audit.get("verdict", "unverified")
        badge = {"verified": ("Price verified", "success"), "caution": ("Verify before buying", "warning")}.get(
            verdict, ("Unverified", "warning")
        )
        main = column(
            result.product,
            best["price"] if best else None,
            best["store"] if best else None,
            reviews.rating if reviews else None,
            (reviews.pros[0] if reviews and reviews.pros else ""),
            badge,
        )
        buttons = [
            {"type": "button", "label": f"Add {result.product[:40]} to cart", "primary": True,
             "action": {"selection": selection("focal_buy_main")}},
        ]
        columns = [main]
        if alt:
            columns.append(
                column(alt.product, alt.price, None, alt_reviews.rating if alt_reviews else alt.rating, alt.reason, ("Top rival", "info"))
            )
            buttons.append({"type": "button", "label": f"Get {alt.product[:40]} instead",
                            "action": {"selection": selection("focal_buy_alt")}})
        buttons.append({"type": "button", "label": "Done for now", "action": {"selection": selection("focal_finish")}})
        flags = audit.get("flags") or []
        payload = {
            "root": {
                "type": "section",
                "title": f"Focal verdict · {result.product}",
                "subtitle": (f"Budget {money(result.max_price)} · " if result.max_price else "")
                + f"{len(self.state.get('team', []))} agent steps",
                "children": [
                    {"type": "group", "direction": "row", "gap": 16, "children": columns},
                    {"type": "divider"},
                    *({"type": "text", "value": f"⚑ {f}", "style": "muted"} for f in flags[:3]),
                    {"type": "group", "direction": "row", "gap": 8, "children": buttons},
                ],
            }
        }
        sources = []
        for section in (price, reviews):
            if section:
                sources += [f"- [{s.title[:60]}]({s.url})" for s in section.sources[:2]]
        await self.show(
            card(
                self.said("focal", "you", "research is done and the prices are verified. Pick one and the buyer agent takes it from here.")
                + "\n\n" + result.summary[:1500]
                + ("\n\n**Sources**\n" + "\n".join(sources) if sources else ""),
                "custom",
                payload,
            )
        )

    # ---- 4. buyer ----------------------------------------------------------------

    @staticmethod
    def amazon_leads(result) -> dict[str, list[dict]]:
        """Amazon product pages the research agents already cited, filed by the product
        their title names (the main pick or the top rival)."""
        from .buyer import amazon_listing
        from .live import matches_product

        products = [result.product]
        if result.alternatives and result.alternatives.alternatives:
            products.append(result.alternatives.alternatives[0].product)
        leads: dict[str, list[dict]] = {p: [] for p in products}
        for section in (result.price, result.reviews, result.alternatives, result.alternative_reviews):
            for source in section.sources if section else []:
                url = amazon_listing(source.url)
                for product in products if url else []:
                    if matches_product(source.title, product) and all(
                        lead["url"] != url for lead in leads[product]
                    ):
                        leads[product].append({"title": source.title, "url": url})
        return leads

    async def on_buy(self, alternative: bool):
        target = self.state.get("alt") if alternative else None
        product = target["product"] if target else self.state.get("product", "")
        self.state["buy_product"] = product
        await self.talk("focal", "buyer", f"find the Amazon listing for **{product}**. Cart only, no checkout.")
        started = time.perf_counter()
        result = await self.ask("buyer", BuyRequest(product=product[:500]), BuyResponse, timeout=60)
        ms = round((time.perf_counter() - started) * 1000)
        candidates = result.candidates if result else []
        leads = (self.state.get("amazon_leads") or {}).get(product) or []
        if not candidates and leads:
            # Recovery: the buyer's own search came back empty, so reuse the Amazon
            # product pages the research agents already cited.
            candidates = leads
            self.note(self.said("buyer", "focal", "my search came back empty, so I'm reusing the Amazon pages the research agents cited."))
        if not candidates:
            self.credit("buyer", "no listing", ms)
            link = result.search_url if result else None
            await self.talk(
                "buyer", "focal",
                "no direct Amazon listing found" + (f"; here's a search link: {link}" if link else "") + ". Skipping the cart.",
                ms,
            )
            return await self.offer_checkout()
        self.credit("buyer", f"{len(candidates)} listings", ms)
        self.state["stage"] = "listing"
        self.save()
        items = [
            {
                "children": [
                    {"type": "text", "value": c["title"][:90] or c["url"], "style": "emphasis"},
                    {"type": "text", "value": c["url"], "style": "muted"},
                ],
                "action": {"selection": selection("focal_pick_listing", url=c["url"])},
            }
            for c in candidates[:5]
        ]
        await self.show(
            card(
                self.said("buyer", "focal", f"found {len(candidates)} Amazon listing(s).", ms)
                + "\n" + self.said("focal", "you", "pick the exact one. Nothing is added yet."),
                "custom",
                {"root": {"type": "section", "title": f"Amazon listings · {product[:60]}",
                          "subtitle": "Cart only. Checkout is disabled.",
                          "children": [{"type": "list", "items": items}]}},
            )
        )

    async def on_listing(self, reply: dict):
        url = reply.get("url")
        if not url:
            return await self.say("Pick one of the listings above.")
        self.note(self.said("focal", "buyer", f"prepare a cart addition for {url}"))
        result = await self.ask(
            "buyer", BuyRequest(product=self.state.get("buy_product", "")[:500] or None, url=url), BuyResponse, timeout=30
        )
        if result is None or not result.confirmation_token:
            return await self.say(f"🛒 {result.summary if result else 'Buyer agent unavailable.'}")
        self.state.update(stage="cart", token=result.confirmation_token, listing=url)
        self.save()
        real = self.sender in owners()
        await self.show(
            card(
                self.said("buyer", "focal", "cart addition ready, waiting for approval.")
                + "\n" + self.said("focal", "you", "approve it?"),
                "review",
                {
                    "title": "Approve cart addition",
                    "summary_rows": [
                        {"label": "Product", "value": self.state.get("buy_product", "")[:80]},
                        {"label": "Listing", "value": url},
                        {"label": "Quantity", "value": str(result.quantity)},
                        {"label": "Expected price", "value": money((self.state.get("best") or {}).get("price"))},
                        {"label": "Checkout", "value": "Disabled: the agent stops at the cart"},
                        {"label": "Mode", "value": "Live Amazon cart (owner)" if real else "Amazon add-to-cart link; you confirm on Amazon"},
                    ],
                    "approve_cta": {"label": "Add to cart", "primary": True, "selection": selection("focal_approve_cart")},
                    "reject_cta": {"label": "Not now", "selection": selection("focal_reject_cart")},
                },
            )
        )

    async def on_cart(self, approved: bool):
        if not approved:
            await self.talk("focal", "buyer", "cancel that; nothing gets added.")
            return await self.offer_watch()
        if self.sender in owners():
            started = time.perf_counter()
            result = await self.ask(
                "buyer", CartApproval(confirmation_token=self.state["token"]), BuyResponse, timeout=120
            )
            ms = round((time.perf_counter() - started) * 1000)
            added = bool(result and result.status == "cart_added")
            self.credit("buyer", result.status if result else "no answer", ms)
            await self.say(
                f"{self.line('buyer')} · {secs(ms)}\n"
                + (f"✅ {result.summary}" + (f"\nCart: {result.cart_url}" if result.cart_url else "") if added
                   else f"⚠️ {result.summary if result else 'No answer from the buyer agent.'}")
            )
        else:
            # Everyone else gets Amazon's own add-to-cart link: they open it and confirm.
            added = await self.cart_link()
        await self.offer_checkout()

    async def cart_link(self) -> bool:
        asin = re.search(r"/dp/([A-Z0-9]{10})", self.state.get("listing") or "", re.IGNORECASE)
        if asin:
            await self.talk("focal", "cart_link", f"approved: build Amazon's add-to-cart link for ASIN {asin[1].upper()}.")
        started = time.perf_counter()
        link = (
            await self.ask("cart_link", CartRequest(product_id=asin[1].upper()), CartLink, timeout=20)
            if asin
            else None
        )
        ms = round((time.perf_counter() - started) * 1000)
        if link is None or not link.cart_url:
            self.credit("cart_link", "no link", ms)
            await self.say(f"🛒 Approval recorded; open the listing to add it yourself: {self.state.get('listing')}")
            return False
        self.credit("cart_link", f"add-to-cart link for {link.product_id}", ms)
        await self.talk(
            "cart_link", "focal",
            f"here's Amazon's official add-to-cart link (you confirm on Amazon; nothing is bought): {link.cart_url}",
            ms,
        )
        return True

    # ---- 5. messenger --------------------------------------------------------------

    # ---- 5. checkout: Agent Payment Protocol (Stripe) ------------------------------

    def purchase(self) -> tuple[str, float | None]:
        product = self.state.get("buy_product") or self.state.get("product", "")
        alt = self.state.get("alt") or {}
        if alt and alt.get("product") == product and alt.get("price"):
            return product, alt["price"]
        return product, (self.state.get("best") or {}).get("price")

    async def offer_checkout(self):
        product, price = self.purchase()
        if not price or not payments.configured():
            self.note(self.said("focal", "you", "no verified price to charge, so I'll skip checkout." if not price
                                else "payments aren't configured, so I'll skip checkout."))
            return await self.offer_watch()
        self.state["stage"] = "checkout"
        self.save()
        test = " (test mode: card 4242 4242 4242 4242, no real money moves)" if payments.test_mode() else ""
        await self.show(
            card(
                self.said("focal", "you", f"ready to check out **{product}** for **{money(price)}**?"),
                "review",
                {
                    "title": f"Checkout · {product[:60]}",
                    "summary_rows": [
                        {"label": "Item", "value": product[:80]},
                        {"label": "Price", "value": money(price)},
                        {"label": "Verified by", "value": "🛡️ Verifier agent"},
                        {"label": "Payment", "value": f"Stripe via the Agent Payment Protocol{test}"},
                    ],
                    "approve_cta": {"label": f"Pay {money(price)}", "primary": True, "selection": selection("focal_checkout")},
                    "reject_cta": {"label": "Not now", "selection": selection("focal_skip_checkout")},
                },
            )
        )

    async def on_checkout(self):
        product, price = self.purchase()
        reference = uuid4().hex
        await self.talk("focal", "stripe", f"open a checkout for **{product}**, {money(price)}.")
        try:
            checkout = await payments.create_checkout(
                f"{product} via FOCAL", reference, cents=round(price * 100), name=product
            )
        except Exception as error:  # noqa: BLE001 -- payment errors are shown, never hidden
            await self.talk("stripe", "focal", f"⚠️ checkout could not start ({error}).")
            return await self.offer_watch()
        self.state.update(stage="awaiting_payment", payment_ref=reference, checkout_id=checkout["id"], paid_amount=price)
        self.save()
        await self.say(
            self.said("stripe", "focal", f"checkout session `{checkout['id'][:18]}…` is ready.")
            + "\n" + self.said("focal", "you", "here's the payment request. Pay in the card below"
                                + (" with test card 4242 4242 4242 4242, any future date, any CVC." if payments.test_mode() else "."))
        )
        # The payment request goes last: ASI:One shows an agent's latest message per turn.
        await self.pace()
        await self.end_stream()
        await self.send(payments.payment_request(str(self.ctx.agent.address), reference, f"{product} via FOCAL", checkout))

    async def show_draft(self, draft, ms: int):
        if draft is None or draft.status != "ready":
            self.credit("messenger", draft.status if draft else "no answer", ms)
            self.note(self.said("messenger", "focal", draft.summary if draft else "unavailable.", ms))
            return await self.finish()
        self.credit("messenger", "draft ready", ms)
        self.state.update(stage="message", draft={"id": draft.draft_id, "text": draft.text})
        self.save()
        await self.show(
            card(
                self.said("messenger", "focal", "draft ready. I won't send it without approval.", ms)
                + "\n" + self.said("focal", "you", "send this?"),
                "review",
                {
                    "title": "Send iMessage?",
                    "summary_rows": [
                        {"label": "To", "value": f"{draft.recipient_name} ({draft.recipient_masked})"},
                        {"label": "Message", "value": draft.text},
                        {"label": "Expires", "value": "5 minutes, single use"},
                    ],
                    "approve_cta": {"label": "Send", "primary": True, "selection": selection("focal_send_message")},
                    "reject_cta": {"label": "Don't send", "selection": selection("focal_skip_message")},
                },
            )
        )

    async def on_message(self, approved: bool):
        draft = self.state.get("draft") or {}
        if not approved:
            self.note(self.said("focal", "messenger", "discard it; don't send."))
        elif self.sender in owners() or imessage_dry_run():
            # In dry-run mode the messenger records the exact send without running it,
            # so anyone can take the real path.
            self.note(self.said("focal", "messenger", "approved, send it."))
            result = await self.ask(
                "messenger", MessageApproval(draft_id=draft["id"], text=draft["text"]), MessageSendResponse, timeout=30
            )
            self.credit("messenger", result.status if result else "no answer")
            dry = " *(dry run: the exact send was recorded, not executed)*" if result and result.dry_run else ""
            self.note(self.said("messenger", "focal", f"{result.summary if result else 'no answer.'}{dry}"))
        else:
            self.credit("messenger", "simulated send")
            self.note(f"{self.line('messenger')}\n🧪 **Simulated:** approval recorded; real sends run only for the owner.")
        await self.finish()

    # ---- 6. wrap-up: watch, message, memory (the shopper picks) ---------------------

    def watch_target(self) -> float | None:
        best = (self.state.get("best") or {}).get("price")
        budget = self.state.get("budget")
        return round(min(x for x in (best and best * 0.95, budget) if x), 2) if (best or budget) else None

    async def offer_watch(self):
        """Last step: one card with the three follow-ups; FOCAL does the ones ticked."""
        product = self.state.get("buy_product") or self.state.get("product", "")
        target = self.watch_target()
        self.state.update(stage="wrapup", watch_target=target)
        self.save()
        bought = "just got" if self.state.get("paid_amount") else "picked"
        fields = []
        if target:
            fields.append({"kind": "checkbox", "name": "watch",
                           "label": f"Keep me updated: alert me if {product[:40]} drops below {money(target)}"})
        fields += [
            {"kind": "checkbox", "name": "message", "label": "Send a message to a friend about it"},
            {"kind": "text", "name": "friend", "label": "Who should I message? (e.g. John)"},
            {"kind": "checkbox", "name": "remember", "label": "Save this to memory for better help next time"},
        ]
        await self.show(
            card(
                self.said("focal", "you", f"all set. Anything else for the {product}?"),
                "form",
                {
                    "title": "Before you go",
                    "fields": fields,
                    "submit_cta": {"label": "Do it", "selection": selection("focal_wrapup", bought=bought)},
                },
            )
        )

    async def on_wrapup(self, reply: dict, body: str):
        def ticked(name: str) -> bool:
            value = reply.get(name)
            if value is None:  # planner prose instead of JSON
                return bool(re.search({"watch": r"\b(watch|updat|alert|price)", "message": r"\b(message|text|tell)",
                                       "remember": r"\b(memory|remember|save)"}[name], body, re.IGNORECASE))
            return str(value).lower() in {"true", "on", "yes", "1"}

        product = self.state.get("buy_product") or self.state.get("product", "")
        if ticked("remember"):
            await self.remember({
                "product": product,
                "budget": self.state.get("budget"),
                "priority": self.state.get("priority"),
                "best_price": (self.state.get("best") or {}).get("price"),
                "bought_for": self.state.get("paid_amount"),
            })
        if ticked("watch") and self.state.get("watch_target"):
            await self.arm_watch()
        if ticked("message"):
            friend = str(reply.get("friend") or "").strip() or os.getenv("FOCAL_SHARE_CONTACT", "John")
            what = "I just got" if self.state.get("paid_amount") else "I'm getting"
            note = f"{what} the {product}! FOCAL found it"
            self.state["standalone"] = True
            await self.talk("focal", "messenger", f"draft a note to {friend}: *{note}*")
            started = time.perf_counter()
            draft = await self.ask("messenger", MessageDraftRequest(request=f"text {friend} {note}"[:1200]),
                                   MessageDraftResponse, timeout=30)
            return await self.show_draft(draft, round((time.perf_counter() - started) * 1000))
        await self.finish()

    async def on_commit(self, msg: CommitPayment):
        """Payment Protocol: verify with Stripe before CompletePayment and the receipt."""
        if self.state.get("stage") != "awaiting_payment" or self.state.get("checkout_id") != msg.transaction_id:
            await self.ctx.send(self.sender, RejectPayment(reason="This payment does not match the pending checkout."))
            return
        if msg.funds.payment_method != "stripe" or not await payments.is_paid(msg.transaction_id):
            await self.ctx.send(self.sender, RejectPayment(reason="Stripe has not confirmed this payment yet."))
            return await self.talk("stripe", "focal", "this session isn't paid yet. Finish checkout, then confirm again.")
        await self.complete_purchase(msg.transaction_id)

    async def check_payment(self, asi_payment_id: str | None = None):
        """ASI:One's confirmation or "I paid": ask Stripe directly, without waiting for
        CommitPayment. In test mode ASI:One's own confirmation is accepted too."""
        session = self.state.get("checkout_id", "")
        await self.talk("focal", "stripe", f"has checkout `{session[:18]}…` been paid?")
        if await payments.is_paid(session):
            return await self.complete_purchase(session)
        if payments.test_mode():
            await self.talk("stripe", "focal", "no card payment recorded on the session yet.")
            return await self.complete_purchase(
                f"asi:{asi_payment_id}" if asi_payment_id else f"confirm:{session}",
                confirmed_by="your confirmation in ASI:One (test mode)",
            )
        await self.talk("stripe", "focal", "not yet. Complete the card form above (test card 4242 4242 4242 4242), then tell me.")

    async def complete_purchase(self, transaction_id: str, confirmed_by: str = "Stripe"):
        # CompletePayment needs an id ASI:One knows: a Stripe session or its payment id.
        if not transaction_id.startswith("confirm:"):
            payment_id = transaction_id.removeprefix("asi:")
            await self.ctx.send(self.sender, CompletePayment(transaction_id=payment_id))
        product, price = self.purchase()
        self.credit("stripe", f"paid {money(price)}")
        await self.talk(
            "stripe" if confirmed_by == "Stripe" else "focal", "focal" if confirmed_by == "Stripe" else "you",
            f"payment `{transaction_id[:22]}…` confirmed by {confirmed_by}: {money(price)}.",
        )
        await self.say(
            self.said("focal", "you", f"🧾 **Purchase complete.** {product} · {money(price)} · paid via the Agent Payment Protocol.")
            + ("\n_Stripe test mode: no real money moved and no real store order was placed._" if payments.test_mode() else "")
        )
        await self.remember({"product": product, "bought_for": price, "kind": "purchase"})
        await self.offer_watch()

    async def arm_watch(self, paid: bool = False):
        watches = self.ctx.storage.get("watches") or {}
        watches[self.sender] = {
            "product": self.state.get("buy_product") or self.state.get("product", ""),
            "target": self.state.get("watch_target"),
            "created": int(time.time()),
            "checks": 0,
            "paid": paid,
        }
        self.ctx.storage.set("watches", watches)
        self.credit("watch", f"armed below {money(self.state.get('watch_target'))}")
        await self.say(
            f"{self.said('focal', 'you', 'watch armed')} for {watches[self.sender]['product']} below {money(self.state.get('watch_target'))}. "
            f"The coordinator re-runs the price and verifier agents every {watch_interval() // 60 or 1} min and "
            "messages you here when it drops."
        )

    # ---- wrap up -------------------------------------------------------------------

    async def finish(self):
        team = self.state.get("team", [])
        rows = []
        for field, did, ms in team:
            emoji, name, _ = AGENTS.get(field, ("•", field, None))
            rows.append({"label": f"{emoji} {name}", "value": did + (f" · {secs(ms)}" if ms else "")})
        self.state["stage"] = "done"
        self.save()
        agents_used = len({AGENTS.get(f, (0, f, 0))[1].split(" (")[0] for f, _, _ in team})
        await self.show(
            card(
                f"{self.said('focal', 'you', 'done.')} {agents_used} agents, {len(team)} steps, one conversation. "
                "Ask FOCAL for anything else any time.",
                "custom",
                {"root": {"type": "section", "title": "Team recap",
                          "children": [
                              {"type": "list", "items": [
                                  {"children": [{"type": "text", "value": r["label"], "style": "emphasis"},
                                                {"type": "text", "value": r["value"], "style": "muted"}]}
                                  for r in rows]}]}},
                terminal=True,
                end_session=True,
            )
        )


def watch_interval() -> int:
    return max(60, int(os.getenv("FOCAL_WATCH_SECONDS", "300")))


def build_asi_protocols(addresses: dict[str, str]) -> list[Protocol]:
    chat = Protocol(spec=chat_protocol_spec)

    @chat.on_message(ChatMessage)
    async def on_chat(ctx: Context, sender: str, msg: ChatMessage):
        await ctx.send(sender, ChatAcknowledgement(timestamp=datetime.now(UTC), acknowledged_msg_id=msg.msg_id))
        # Direct @mentions arrive as "@agent1q… <text>"; card selections follow the mention.
        body = re.sub(r"^\s*(?:@\S+\s*)+", "", message_text(msg))
        if not body:
            return
        ctx.logger.info(f"ASI:One chat from {sender} (session {ctx.session}): {body[:300]!r}")
        conversation = Conversation(ctx, sender, addresses)
        try:
            await conversation.handle(body)
            await conversation.end_stream()
        except Exception:
            ctx.logger.exception("Conversation step failed")
            await conversation.say("⚠️ Something went wrong on my side. Please ask again.", end=True)

    @chat.on_message(ChatAcknowledgement)
    async def on_ack(ctx: Context, sender: str, msg: ChatAcknowledgement):
        pass

    pay = Protocol(spec=payment_protocol_spec, role="seller")

    @pay.on_message(CommitPayment)
    async def on_commit(ctx: Context, sender: str, msg: CommitPayment):
        await Conversation(ctx, sender, addresses).on_commit(msg)

    @pay.on_message(RejectPayment)
    async def on_reject(ctx: Context, sender: str, msg: RejectPayment):
        conversation = Conversation(ctx, sender, addresses)
        await conversation.say("💳 Payment declined. No watch was started.")
        await conversation.finish()

    return [chat, pay]


def add_watch_loop(agent, addresses: dict[str, str]):
    @agent.on_interval(period=watch_interval())
    async def check_watches(ctx: Context):
        watches = ctx.storage.get("watches") or {}
        for sender, watch in list(watches.items()):
            if PRICE not in addresses:
                return
            price, _ = await ctx.send_and_receive(
                addresses[PRICE], PriceCompareRequest(product=watch["product"]), PriceCompareResponse, timeout=60
            )
            if not isinstance(price, PriceCompareResponse) or VERIFIER not in addresses:
                continue
            audit, _ = await ctx.send_and_receive(
                addresses[VERIFIER], VerifyRequest(price=price, max_price=watch["target"]), VerifyResponse, timeout=20
            )
            watch["checks"] += 1
            if isinstance(audit, VerifyResponse) and audit.best and audit.best.price <= watch["target"]:
                await ctx.send(
                    sender,
                    cards.text(
                        f"🔔 **Price drop:** {watch['product']} is {money(audit.best.price)} at {audit.best.store} "
                        f"(verified by the verifier agent, target {money(watch['target'])}).\n{audit.best.url}",
                        end_session=True,
                    ),
                )
                del watches[sender]
        ctx.storage.set("watches", watches)
