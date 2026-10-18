import json
from uuid import uuid4

import httpx
import pytest
from test_orchestrator import api, config  # noqa: F401 -- api is a fixture

from laai_orchestrator.models import ChatMessage
from laai_orchestrator.scoped import AgentName, pasted_offers, scoped_chat

USER = uuid4()
TOKEN = "t" * 32


def recorder():
    seen = []

    def handle(request):
        seen.append((request.url.path, json.loads(request.content)))
        return httpx.Response(
            200, json={"summary": "ok", "product": "Sony XM5", "purchase_enabled": False}
        )

    return seen, httpx.AsyncClient(transport=httpx.MockTransport(handle))


async def ask(agent, text, history=(), **kwargs):
    seen, http = recorder()
    messages = [*history, ChatMessage(role="user", content=text)]
    async with http:
        reply = await scoped_chat(agent, messages, config(), http, user=USER, **kwargs)
    return reply, seen


@pytest.mark.parametrize(
    "agent, text, route",
    [
        (AgentName.PRICE, "Consider Sony XM5 and recommend an alternative under $300", "/price"),
        (AgentName.ALTERNATIVES, "Sony XM5 under $300", "/alternatives"),
        (AgentName.REVIEWS, "Is the Sony XM5 worth it? Check prices and alternatives", "/reviews"),
        (AgentName.YOUTUBE, "Find the song Blinding Lights", "/youtube"),
        (AgentName.NEWS, "What's the latest on Sony XM5 prices?", "/news"),
        (AgentName.MESSENGER, "Text Alex that I'm running late", "/imessage/prepare"),
        (AgentName.BUYER, "Add the Sony XM5 to my cart", "/buy/prepare"),
        (AgentName.VERIFIER, "Sony XM5: Amazon $279, Best Buy $299", "/verify"),
        (AgentName.MEMORY, "What do you know about me?", "/memory"),
    ],
)
async def test_specialist_chat_calls_only_its_own_agent(agent, text, route):
    # Even text that would make the main chat orchestrate stays with this one agent.
    reply, seen = await ask(agent, text)
    assert [path for path, _ in seen] == [route]
    assert reply.source == "agent" and len(reply.calls) == 1


async def test_alternatives_chat_reads_the_budget():
    _, seen = await ask(AgentName.ALTERNATIVES, "Sony XM5 but under $250")
    assert seen[0][1]["max_price"] == 250


async def test_coordinator_chat_still_orchestrates():
    reply, seen = await ask(AgentName.COORDINATOR, "Consider Sony XM5 under $300")
    assert [path for path, _ in seen] == ["/consider"]
    assert reply.source == "coordinator"


async def test_verifier_audits_only_pasted_offers():
    reply, seen = await ask(AgentName.VERIFIER, "Is the Sony XM5 deal real?")
    assert seen == [] and "Paste the offers" in reply.reply
    _, seen = await ask(
        AgentName.VERIFIER, "Sony XM5: Amazon $279.99, dealz4u $99 https://dealz4u.shop/x, max $300"
    )
    payload = seen[0][1]
    assert payload["max_price"] == 300
    offers = payload["price"]["offers"]
    assert [(o["store"], o["price"], o["trust"]) for o in offers] == [
        ("Amazon", 279.99, "medium"),
        ("dealz4u", 99, "low"),
    ]
    assert offers[1]["url"] == "https://dealz4u.shop/x"
    assert all(o["source"] == "user_pasted" for o in offers)


def test_pasted_offers_without_a_product_label():
    product, offers = pasted_offers("Walmart $310; Target $305")
    assert product == "the pasted offers" and [o["store"] for o in offers] == ["Walmart", "Target"]


async def test_memory_chat_saves_and_recalls_per_user():
    _, seen = await ask(AgentName.MEMORY, "Remember that I prefer over-ear headphones")
    assert seen[0][1] == {
        "user": f"dashboard:{USER}",
        "remember": {"note": "I prefer over-ear headphones"},
    }
    _, seen = await ask(AgentName.MEMORY, "What do you remember?")
    assert seen[0][1]["remember"] is None


async def test_buyer_chat_confirms_only_an_approved_review_token():
    reply, seen = await ask(AgentName.BUYER, "confirm add to cart")
    assert seen == [] and "Nothing was added" in reply.reply
    _, seen = await ask(AgentName.BUYER, "confirm add to cart", purchase_confirmation=TOKEN)
    assert [path for path, _ in seen] == ["/buy/confirm"]
    assert seen[0][1]["confirmation_token"] == TOKEN
    # A new product request with a waiting token prepares a review; it never confirms.
    _, seen = await ask(AgentName.BUYER, "Sony XM5", purchase_confirmation=TOKEN)
    assert [path for path, _ in seen] == ["/buy/prepare"]


async def test_messenger_chat_only_drafts_even_for_yes():
    history = [ChatMessage(role="assistant", content="What would you like to say to Alex?")]
    _, seen = await ask(AgentName.MESSENGER, "running late", history)
    assert seen == [("/imessage/prepare", {"request": "text Alex running late"})]
    _, seen = await ask(AgentName.MESSENGER, "yes send it")
    assert [path for path, _ in seen] == ["/imessage/prepare"]


def test_scoped_stream_requires_sign_in_and_a_known_agent(api):  # noqa: F811
    client, backend = api
    body = {"messages": [{"role": "user", "content": "Sony XM5"}]}
    response = client.post(
        "/v1/agents/laai-reviews/chat/stream", json=body, headers={"Authorization": "Bearer bad"}
    )
    assert response.status_code == 401 and backend.agent_calls == []
    assert client.post("/v1/agents/laai-unknown/chat/stream", json=body).status_code == 422
    response = client.post("/v1/agents/laai-reviews/chat/stream", json=body)
    assert response.status_code == 200
    events = [json.loads(line[5:]) for line in response.text.splitlines() if line.startswith("data:")]
    result = events[-1]
    assert result["type"] == "result" and result["data"]["source"] == "agent"
    assert {path for path, _ in backend.agent_calls} <= {"/reviews", "/progress"}
    assert "/reviews" in {path for path, _ in backend.agent_calls}
