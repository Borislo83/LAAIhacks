import json

import pytest
from uagents_core.contrib.protocols.chat import (
    ChatMessage,
    EndSessionContent,
    MetadataContent,
    TextContent,
)

from laai_agents import asi_flow, cards, handlers, imessage, memory, verifier
from laai_agents.buyer import BuyRequest, BuyResponse, CartApproval
from laai_agents.models import (
    AlternativesRequest,
    CartRequest,
    Offer,
    PriceCompareRequest,
    PriceCompareResponse,
    ReviewsRequest,
)

OWNER = "agent1qowner"
SHOPPER = "agent1qshopper"


@pytest.fixture(autouse=True)
def offline(monkeypatch):
    monkeypatch.delenv("ASI_ONE_API_KEY", raising=False)
    monkeypatch.delenv("STRIPE_SECRET_KEY", raising=False)
    monkeypatch.delenv("SUPABASE_URL", raising=False)
    monkeypatch.setenv("PRODUCT_DATA_MODE", "demo")
    monkeypatch.setenv("IMESSAGE_CONTACTS", "Alex=+15551234567")
    monkeypatch.setenv("FOCAL_SHARE_CONTACT", "Alex")
    monkeypatch.setenv("FOCAL_OWNER_ADDRESSES", OWNER)
    monkeypatch.setattr(asi_flow, "CHUNK_GAP", 0)


class Storage(dict):
    def set(self, key, value):
        self[key] = json.loads(json.dumps(value))


class Agent:
    address = "agent1qcoordinator"


class Context:
    """Routes typed messages to the real handlers, like the Bureau would."""

    def __init__(self):
        self.storage = Storage()
        self.sent = []
        self.cart_approvals = []
        self.agent = Agent()

    async def send(self, destination, message):
        self.sent.append(message)

    async def send_and_receive(self, destination, message, response_type, timeout):
        if isinstance(message, PriceCompareRequest):
            return await handlers.compare_prices(message), None
        if isinstance(message, ReviewsRequest):
            return await handlers.get_reviews(message), None
        if isinstance(message, AlternativesRequest):
            return await handlers.find_alternatives(message), None
        if isinstance(message, verifier.VerifyRequest):
            return await verifier.verify(message), None
        if isinstance(message, memory.MemoryRequest):
            return await memory.handle(self, message), None
        if isinstance(message, BuyRequest):
            url = message.url or "https://www.amazon.com/dp/B09XS7JWHH"
            return BuyResponse(
                product=message.product or "Sony WH-1000XM5",
                status="cart_review" if message.url else "choose_listing",
                candidates=[] if message.url else [{"title": "Sony XM5", "url": url}],
                confirmation_token="c" * 32 if message.url else None,
                summary="ok",
            ), None
        if isinstance(message, CartRequest):
            from laai_agents.carts import _response, amazon_cart_link

            return _response("amazon", message, amazon_cart_link(message.product_id, message.quantity)), None
        if isinstance(message, CartApproval):
            self.cart_approvals.append(message)
            return BuyResponse(product="Sony", status="cart_added", summary="Added to cart."), None
        if isinstance(message, imessage.MessageDraftRequest):
            return await imessage.prepare(message), None
        raise AssertionError(f"unexpected {message!r}")

    def card_kinds(self):
        return [
            item.metadata["card_kind"]
            for msg in self.sent
            if isinstance(msg, ChatMessage)
            for item in msg.content
            if isinstance(item, MetadataContent)
        ]

    def texts(self):
        return "\n".join(
            item.text
            for msg in self.sent
            if isinstance(msg, ChatMessage)
            for item in msg.content
            if isinstance(item, TextContent)
        )


ADDRESSES = {
    name: name
    for name in (
        "laai-price-compare",
        "laai-reviews",
        "laai-alternatives",
        "focal-price-verifier",
        "focal-memory",
        "focal-amazon-buyer-demo",
        "laai-messenger",
        "laai-amazon-cart",
    )
}


async def step(ctx, sender, body):
    await asi_flow.Conversation(ctx, sender, ADDRESSES).handle(body)


async def test_full_flow_from_intent_to_watch_with_cards():
    ctx = Context()
    await step(ctx, SHOPPER, "Should I buy the Sony XM5?")
    assert ctx.card_kinds() == ["form"]
    assert "Memory agent" in ctx.texts() and "laai_user_preferences" in ctx.texts()

    await step(ctx, SHOPPER, json.dumps({"focal_action": "focal_submit_brief", "budget": "400", "priority": "price"}))
    text = ctx.texts()
    for agent in ("Price agent", "Reviews agent", "Alternatives agent", "Verifier agent"):
        assert agent in text
    assert ctx.card_kinds()[-1] == "custom"

    await step(ctx, SHOPPER, json.dumps({"focal_action": "focal_buy_main"}))
    await step(ctx, SHOPPER, json.dumps({"focal_action": "focal_pick_listing", "url": "https://www.amazon.com/dp/B09XS7JWHH"}))
    assert ctx.card_kinds()[-1] == "review"

    await step(ctx, SHOPPER, "The user approved: focal_approve_cart")
    assert not ctx.cart_approvals, "non-owners never trigger the real cart"
    assert "add-to-cart link" in ctx.texts()
    # Payments are not configured here, so checkout is skipped straight to the wrap-up form.
    assert ctx.card_kinds()[-1] == "form"

    await step(ctx, SHOPPER, json.dumps(
        {"focal_action": "focal_wrapup", "watch": True, "message": True, "friend": "Alex", "remember": True}
    ))
    assert SHOPPER in ctx.storage["watches"]
    assert "saved to the profile" in ctx.texts()
    assert ctx.card_kinds()[-1] == "review"  # iMessage draft, only because it was asked for

    await step(ctx, SHOPPER, json.dumps({"focal_action": "focal_send_message"}))
    last = ctx.sent[-1]
    assert any(isinstance(i, EndSessionContent) for i in last.content)
    assert ctx.card_kinds()[-1] == "custom"  # team recap


async def test_owner_approval_reaches_buyer_agent():
    ctx = Context()
    conversation = asi_flow.Conversation(ctx, OWNER, ADDRESSES)
    conversation.state = {"stage": "cart", "token": "c" * 32, "team": [], "product": "Sony WH-1000XM5"}
    await conversation.handle("yes")
    assert ctx.cart_approvals[0].confirmation_token == "c" * 32
    assert "Added to cart" in ctx.texts()


async def test_verifier_drops_low_trust_and_requests_retry():
    offer = Offer(product="X", store="sketchy.biz", price=10, url="https://sketchy.biz", trust="low")
    audit = await verifier.verify(
        verifier.VerifyRequest(price=PriceCompareResponse(product="X", offers=[offer], summary=""))
    )
    assert audit.needs_retry and audit.dropped[0].store == "sketchy.biz" and audit.best is None


async def test_verifier_flags_outliers_and_budget():
    offers = [
        Offer(product="X", store=s, price=p, url=f"https://{s}.com")
        for s, p in (("amazon", 100), ("bestbuy", 104), ("target", 102), ("walmart", 30))
    ]
    audit = await verifier.verify(
        verifier.VerifyRequest(price=PriceCompareResponse(product="X", offers=offers, summary=""), max_price=90)
    )
    assert {d.store for d in audit.dropped} == {"walmart"}
    assert audit.best.store == "amazon" and audit.within_budget is False


def test_parse_reply_reads_json_and_planner_prose():
    assert cards.parse_reply('{"focal_action": "focal_buy_alt"}')["focal_action"] == "focal_buy_alt"
    prose = cards.parse_reply("User picked https://www.amazon.com/dp/B09XS7JWHH (focal_pick_listing), budget $1,200")
    assert prose == {
        "focal_action": "focal_pick_listing",
        "url": "https://www.amazon.com/dp/B09XS7JWHH",
        "budget": "1200",
    }


def test_card_metadata_values_are_strings():
    msg = cards.brief_form("Sony", "balanced")
    meta = next(i for i in msg.content if isinstance(i, MetadataContent)).metadata
    assert all(isinstance(v, str) for v in meta.values())
    assert meta["card_protocol_version"] == "1" and json.loads(meta["card_payload"])["fields"]


async def test_checkout_charges_the_product_price_and_verifies_with_stripe(monkeypatch):
    from uagents_core.contrib.protocols.payment import (
        CommitPayment,
        CompletePayment,
        Funds,
        RequestPayment,
    )

    from laai_agents import payments

    monkeypatch.setenv("STRIPE_SECRET_KEY", "sk_test_x")
    monkeypatch.setenv("STRIPE_PUBLISHABLE_KEY", "pk_test_x")
    created = {}

    async def checkout(description, reference, cents=None, name=""):
        created.update(cents=cents, name=name)
        return {"id": "cs_test_1", "client_secret": "s", "publishable_key": "pk_test_x", "amount_cents": str(cents)}

    paid = {"cs_test_1": False}

    async def is_paid(session_id):
        return paid.get(session_id, False)

    monkeypatch.setattr(payments, "create_checkout", checkout)
    monkeypatch.setattr(payments, "is_paid", is_paid)
    ctx = Context()
    conversation = asi_flow.Conversation(ctx, SHOPPER, ADDRESSES)
    conversation.state = {"stage": "checkout", "product": "Apple AirPods Pro 2", "team": [],
                          "best": {"price": 249.31}}
    await conversation.handle(json.dumps({"focal_action": "focal_checkout"}))
    request = next(m for m in ctx.sent if isinstance(m, RequestPayment))
    assert created == {"cents": 24931, "name": "Apple AirPods Pro 2"}
    assert request.accepted_funds[0].amount == "249.31" and request.metadata["stripe"]["id"] == "cs_test_1"
    assert isinstance(ctx.sent[-1], RequestPayment), "the payment request must be the last message"

    commit = CommitPayment(funds=Funds(amount="249.31", currency="USD", payment_method="stripe"),
                           recipient="x", transaction_id="cs_test_1")
    await asi_flow.Conversation(ctx, SHOPPER, ADDRESSES).on_commit(commit)
    assert not any(isinstance(m, CompletePayment) for m in ctx.sent), "unpaid checkout must not complete"

    paid["cs_test_1"] = True
    await asi_flow.Conversation(ctx, SHOPPER, ADDRESSES).on_commit(commit)
    assert any(isinstance(m, CompletePayment) for m in ctx.sent)
    assert "Purchase complete" in ctx.texts() and ctx.card_kinds()[-1] == "form"


async def test_typed_confirmation_passes_in_test_mode(monkeypatch):
    from uagents_core.contrib.protocols.payment import CompletePayment

    from laai_agents import payments

    monkeypatch.setenv("STRIPE_SECRET_KEY", "sk_test_x")
    monkeypatch.setenv("STRIPE_PUBLISHABLE_KEY", "pk_test_x")

    async def unpaid(session_id):
        return False

    monkeypatch.setattr(payments, "is_paid", unpaid)
    ctx = Context()
    conversation = asi_flow.Conversation(ctx, SHOPPER, ADDRESSES)
    conversation.state = {"stage": "awaiting_payment", "checkout_id": "cs_test_9", "product": "Apple AirPods Pro 2",
                          "team": [], "best": {"price": 249.31}}
    await conversation.handle("stripe:payment_id:1b949186-eb30-465b-a9d6-7091abe4779d:CONFIRM")
    assert any(isinstance(m, CompletePayment) for m in ctx.sent)
    assert "test mode" in ctx.texts()
