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
    PriceCompareRequest,
    PriceCompareResponse,
)
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
}
# Progress stages from inside the specialists worth narrating, once per agent.
LIVE_STAGES = {"fallback", "retrying", "sources_found"}


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
        self.log: list[str] = []
        self.last_send = 0.0

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
        """Add to the running log without sending: the card that follows carries it.
        ASI:One drops a card that closely follows another message from the same step."""
        self.log.append(body)

    async def pace(self):
        """ASI:One keeps one of two messages that arrive within moments of each other,
        so a card or payment request waits a beat after the previous update."""
        wait = 1.2 - (time.monotonic() - self.last_send)
        if wait > 0:
            await asyncio.sleep(wait)

    async def send(self, msg):
        await self.ctx.send(self.sender, msg)
        self.last_send = time.monotonic()

    async def say(self, body: str, end: bool = False):
        self.log.append(body)
        await self.send(cards.text("\n\n".join(self.log), end_session=end))

    async def show(self, msg: ChatMessage):
        """Send a card whose narration is the running log; nothing follows it this turn."""
        for item in msg.content:
            if isinstance(item, TextContent):
                self.log.append(item.text)
                item.text = "\n\n".join(self.log)
        await self.pace()
        await self.send(msg)

    def credit(self, field: str, did: str, ms: int | None = None):
        self.state.setdefault("team", []).append([field, did, ms])

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
        if stage == "watch_offer" and action == "focal_start_watch":
            return await self.on_watch()
        if action == "focal_finish":
            return await self.finish()
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
            "message": lambda: self.on_message(True),
            "watch_offer": self.on_watch,
        }
        return await routes.get(stage, self.finish)()

    async def on_no(self, stage: str):
        routes = {
            "cart": lambda: self.on_cart(False),
            "message": lambda: self.on_message(False),
        }
        return await routes.get(stage, self.finish)()

    # ---- 1. memory + brief -------------------------------------------------------

    async def start(self, body: str):
        from .coordinator import budget_from, parse_request

        req = await parse_request(body)
        if req is not None and re.search(r"\bno product\b|not (?:provided|specified|mentioned)", req.product, re.IGNORECASE):
            req = None
        if req is None:
            return await self.say(
                "I'm **Focal**, a team of shopping agents. Tell me a product, e.g. "
                "*\"Should I buy the Sony WH-1000XM5 under $350?\"* and I'll research, verify, "
                "add it to your cart, tell a friend and watch the price."
            )
        self.reset()
        self.state.update(product=req.product, budget=req.max_price or budget_from(body))
        await self.say(
            f"🎯 **Intent:** research and decide on **{req.product}**"
            + (f" under {money(self.state['budget'])}" if self.state["budget"] else "")
            + ".\nAsking the memory agent what it knows about you…"
        )
        started = time.perf_counter()
        memory = await self.ask("memory", MemoryRequest(user=self.shopper), MemoryResponse, timeout=15)
        ms = round((time.perf_counter() - started) * 1000)
        priority = "balanced"
        if memory:
            priority = memory.preferences.get("shopping_priority", "balanced")
            self.credit("memory", memory.summary, ms)
            status = f"HTTP {memory.status}" if memory.status else "local"
            await self.say(
                f"{self.line('memory')} · {secs(ms)}\n`{memory.request}` → {status}\n"
                + code_block(memory.response)
                + f"\n{memory.summary}"
            )
        else:
            await self.say("🧠 Memory agent unavailable; continuing with default preferences.")
        self.state["priority"] = priority
        if self.state["budget"] is None:
            self.state["stage"] = "brief"
            self.save()
            return await self.show(cards.brief_form(req.product, priority))
        self.save()
        await self.research()

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
            f"🗺️ **Plan for {product}**"
            + (f" · budget {money(budget)}" if budget else "")
            + f" · priority **{self.state.get('priority', 'balanced')}**\n"
            f"1. {self.line('price')}: search every store, read prices from product pages\n"
            f"2. {self.line('reviews')}: average expert ratings *(parallel)*\n"
            f"3. {self.line('alternatives')} → ⭐ Reviews: rivals under budget, then review the top rival\n"
            f"4. {self.line('verification')}: audit every price; re-dispatch if nothing is trustworthy\n"
            "5. You decide → 🛒 Buyer → 💬 Messenger → 👀 Price watch *(only after you approve)*\n\n"
            "Dispatching 3 agents in parallel now…"
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
            )
        finally:
            stop.set()
            await forwarder
        await self.present(result)

    async def forward_progress(self, progress_id: str, stop: asyncio.Event):
        after, said = 0, set()
        while True:
            for event in read(progress_id, after):
                after = event["seq"]
                key = (event["kind"], event["stage"])
                if event["stage"] in LIVE_STAGES and (key not in said or event["stage"] == "fallback"):
                    said.add(key)
                    field = event["kind"] if event["kind"] in AGENTS else None
                    who = f"{AGENTS[field][0]} {AGENTS[field][1]}" if field else event["kind"]
                    icon = {"fallback": "↪️", "retrying": "🔁"}.get(event["stage"], "🔎")
                    await self.say(f"{icon} {who}: {event['message']}")
            if stop.is_set():
                return
            await asyncio.sleep(0.4)

    async def on_update(self, field: str, result, ms: int):
        if field not in AGENTS:
            return
        head = f"{self.line(field)} · {secs(ms)}"
        if result is None:
            self.credit(field, "no answer", ms)
            return await self.say(f"⚠️ {head}\nNo answer after 3 attempts; continuing without it. Nothing was made up.")
        if getattr(result, "error", None):
            self.credit(field, result.error, ms)
            return await self.say(f"⚠️ {head}\n{result.error}")
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
            if result.needs_retry:
                body += "\n🔁 **Recovery:** re-dispatching the price agent with a deeper store search…"
        else:
            did, body = "done", getattr(result, "summary", "")
        self.credit(field, did, ms)
        await self.say(f"{head}\n{body}")

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
        await self.ask(
            "memory",
            MemoryRequest(
                user=self.shopper,
                remember={
                    "product": result.product,
                    "budget": result.max_price,
                    "priority": self.state.get("priority"),
                    "best_price": best["price"] if best else None,
                },
            ),
            MemoryResponse,
            timeout=10,
        )
        self.log.append(f"{self.line('memory')}: saved this research to your profile.")
        await self.show(
            card(
                "✅ **Research complete.** Here's the verified comparison. Pick one and the "
                "buyer agent takes it from here.\n\n" + result.summary[:1500]
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
        await self.say(f"{self.line('buyer')}: finding Amazon listings for **{product}**…")
        started = time.perf_counter()
        result = await self.ask("buyer", BuyRequest(product=product[:500]), BuyResponse, timeout=60)
        ms = round((time.perf_counter() - started) * 1000)
        candidates = result.candidates if result else []
        leads = (self.state.get("amazon_leads") or {}).get(product) or []
        if not candidates and leads:
            # Recovery: the buyer's own search came back empty, so reuse the Amazon
            # product pages the research agents already cited.
            candidates = leads
            self.note("🛒 Buyer search found nothing new; reusing Amazon listings the research agents cited.")
        if not candidates:
            self.credit("buyer", "no listing", ms)
            link = result.search_url if result else None
            await self.say(
                "🛒 No direct Amazon listing found"
                + (f"; search here: {link}" if link else "")
                + ". Skipping the cart step."
            )
            return await self.offer_message(added=False)
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
                f"{self.line('buyer')} · {secs(ms)}\nFound {len(candidates)} Amazon listings. "
                "Pick the exact one; nothing is added yet.",
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
                f"{self.line('buyer')}: cart addition prepared. Approve to add it.",
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
            await self.say("🛒 Cart step skipped. Nothing was added.")
            return await self.offer_message(added=False)
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
        await self.offer_message(added=added)

    async def cart_link(self) -> bool:
        asin = re.search(r"/dp/([A-Z0-9]{10})", self.state.get("listing") or "", re.IGNORECASE)
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
        await self.say(
            f"{self.line('cart_link')} · {secs(ms)}\n✅ Approved. Amazon's official add-to-cart link "
            f"(you confirm on Amazon; nothing is bought):\n{link.cart_url}"
        )
        return True

    # ---- 5. messenger --------------------------------------------------------------

    async def offer_message(self, added: bool):
        friend = os.getenv("FOCAL_SHARE_CONTACT", "").strip()
        product = self.state.get("buy_product") or self.state.get("product", "")
        if not friend:
            self.note("💬 Messenger skipped: set FOCAL_SHARE_CONTACT to tell a friend.")
            return await self.offer_watch()
        price = (self.state.get("best") or {}).get("price")
        note = f"just {'added' if added else 'picked'} the {product}" + (f" for {money(price)}" if price else "") + " (Focal found it)"
        started = time.perf_counter()
        draft = await self.ask(
            "messenger", MessageDraftRequest(request=f"text {friend} {note}"[:1200]), MessageDraftResponse, timeout=30
        )
        ms = round((time.perf_counter() - started) * 1000)
        if draft is None or draft.status != "ready":
            self.credit("messenger", draft.status if draft else "no answer", ms)
            await self.say(f"💬 {draft.summary if draft else 'Messenger agent unavailable.'}")
            return await self.offer_watch()
        self.credit("messenger", "draft ready", ms)
        self.state.update(stage="message", draft={"id": draft.draft_id, "text": draft.text})
        self.save()
        await self.show(
            card(
                f"{self.line('messenger')} · {secs(ms)}\nDrafted a message. Nothing is sent until you approve.",
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
            self.note("💬 Message discarded. Nothing was sent.")
        elif self.sender in owners() or imessage_dry_run():
            # In dry-run mode the messenger records the exact send without running it,
            # so anyone can take the real path.
            result = await self.ask(
                "messenger", MessageApproval(draft_id=draft["id"], text=draft["text"]), MessageSendResponse, timeout=30
            )
            self.credit("messenger", result.status if result else "no answer")
            dry = " *(dry run: the exact send was recorded, not executed)*" if result and result.dry_run else ""
            self.note(f"{self.line('messenger')}\n{result.summary if result else 'No answer.'}{dry}")
        else:
            self.credit("messenger", "simulated send")
            self.note(f"{self.line('messenger')}\n🧪 **Simulated:** approval recorded; real sends run only for the owner.")
        await self.offer_watch()

    # ---- 6. paid price watch (last) ----------------------------------------------

    async def offer_watch(self):
        best = (self.state.get("best") or {}).get("price")
        budget = self.state.get("budget")
        target = round(min(x for x in (best and best * 0.95, budget) if x), 2) if (best or budget) else None
        if target is None:
            return await self.finish()
        self.state.update(stage="watch_offer", watch_target=target)
        self.save()
        paid = payments.configured()
        fee = f"${payments.amount_cents() / 100:.2f}"
        await self.show(
            card(
                "👀 **Last step:** want me to keep watching the price after this chat ends?",
                "review",
                {
                    "title": "Focal Pro price watch",
                    "summary_rows": [
                        {"label": "Product", "value": self.state.get("buy_product") or self.state.get("product", "")},
                        {"label": "Alert me below", "value": money(target)},
                        {"label": "Checks", "value": f"Every {watch_interval() // 60 or 1} min via the price + verifier agents"},
                        {"label": "Price", "value": f"{fee} one-time via Stripe" + (" (test mode, card 4242 4242 4242 4242)" if payments.test_mode() else "") if paid else "Free (payments not configured)"},
                    ],
                    "approve_cta": {"label": f"Start watch ({fee})" if paid else "Start watch", "primary": True,
                                    "selection": selection("focal_start_watch")},
                    "reject_cta": {"label": "No thanks", "selection": selection("focal_finish")},
                },
            )
        )

    async def on_watch(self):
        if not payments.configured():
            await self.arm_watch()
            return await self.finish()
        reference = uuid4().hex
        product = self.state.get("buy_product") or self.state.get("product", "")
        try:
            checkout = await payments.create_checkout(f"Price watch for {product}", reference)
        except Exception as error:  # noqa: BLE001 -- payment errors are shown, never hidden
            await self.say(f"💳 Stripe checkout could not start ({error}). Starting the watch free instead.")
            await self.arm_watch()
            return await self.finish()
        self.state.update(stage="awaiting_payment", payment_ref=reference, checkout_id=checkout["id"])
        self.save()
        self.ctx.storage.set(f"payment:{reference}", self.sender)
        # The payment request goes last: ASI:One shows an agent's latest message per turn.
        await self.say(
            "💳 **Payment Protocol:** sending a Stripe payment request"
            + (" in **test mode**. Use card 4242 4242 4242 4242, any future date and any CVC." if payments.test_mode() else ".")
            + " The watch starts as soon as Stripe confirms."
        )
        await self.pace()
        await self.send(
            payments.payment_request(
                str(self.ctx.agent.address), reference, f"Focal Pro price watch for {product}", checkout
            ),
        )

    async def on_commit(self, msg: CommitPayment):
        """Payment Protocol: verify with Stripe before CompletePayment and the watch."""
        if self.state.get("stage") != "awaiting_payment" or self.state.get("checkout_id") != msg.transaction_id:
            await self.ctx.send(self.sender, RejectPayment(reason="This payment does not match the pending watch."))
            return
        if msg.funds.payment_method != "stripe" or not await payments.is_paid(msg.transaction_id):
            await self.ctx.send(self.sender, RejectPayment(reason="Stripe has not confirmed this payment yet."))
            return await self.say("💳 Stripe hasn't confirmed the payment yet. Finish checkout, then approve again.")
        await self.ctx.send(self.sender, CompletePayment(transaction_id=msg.transaction_id))
        await self.say("💳 **Payment verified with Stripe.** CompletePayment sent.")
        await self.arm_watch(paid=True)
        await self.finish()

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
            f"👀 **Watch armed** for {watches[self.sender]['product']} below {money(self.state.get('watch_target'))}. "
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
                f"🏁 **Done.** {agents_used} agents, {len(team)} steps, one conversation. "
                "Ask about another product any time.",
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
