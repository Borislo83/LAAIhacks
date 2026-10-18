"""Which agent each chat phrase reaches, with and without an ASI:One key.

Every agent entry point is replaced by a recorder and the HTTP client refuses all
requests, so nothing here can play, open, send or add to a cart.
"""

import httpx
import pytest

from laai_orchestrator import chat as chat_module
from laai_orchestrator.chat import _is_message_request
from laai_orchestrator.config import Config
from laai_orchestrator.models import AgentCall, ChatMessage, ChatReply

# Route labels: agent kinds joined by "+", in call order. "spotify:<action>" for Spotify,
# "play" when the found video is opened on the laptop, "llm" for the ASI:One path,
# "none" when the rules answer without any agent.
SHOP = "price+alternatives+reviews"
SAME = object()  # the LLM-key route is the keyword route (fast path or rules decide first)

MATRIX = [
    # Shopping and prices
    ("Sony WH-1000XM5", SHOP, "llm"),
    ("price of the AirPods Pro 2", "price", SAME),
    ("how much is the Nintendo Switch 2", "price", SAME),
    ("where can I buy a Kindle Paperwhite", "price", SAME),
    ("cheapest price for Sony WH-1000XM5", "price+reviews", "llm"),
    ("is the Sony WH-1000XM5 a good deal", "price+reviews", "llm"),
    ("buy the Kindle Paperwhite", "buyer", SAME),
    ("add the Sony XM5 to my cart", "buyer", SAME),
    # Reviews
    ("AirPods Pro 2 reviews", "reviews", "llm"),
    ("show me the reviews for the Sony XM5", "reviews", SAME),
    ("are the AirPods Pro 2 worth it", "reviews", "llm"),
    # Rivals and alternatives
    ("cheaper alternatives to Sony WH-1000XM5", "price+alternatives", SAME),
    ("compare Sony WH-1000XM5 with rivals", "price+alternatives", SAME),
    ("compare Sony WH-1000XM5 vs Bose QuietComfort Ultra", "price+price", SAME),
    # Coordinator: consider, budget, "like the", store constraint
    ("alternatives to the AirPods Pro 2", "consider", SAME),
    ("consider the Sony WH-1000XM5", "consider", SAME),
    ("noise cancelling headphones under $200", "consider", SAME),
    ("headphones like the AirPods Max", "consider", SAME),
    ("smart watches on Amazon", "consider", SAME),
    # Spotify controls
    ("pause", "spotify:pause", SAME),
    ("pause the music", "spotify:pause", SAME),
    ("next song", "spotify:next", SAME),
    ("skip", "spotify:next", SAME),
    ("previous track", "spotify:previous", SAME),
    ("resume", "spotify:resume", SAME),
    ("volume 40", "spotify:volume", SAME),
    ("set volume to 40", "spotify:volume", SAME),
    ("turn it up", "spotify:volume", SAME),
    ("turn it down", "spotify:volume", SAME),
    ("what's playing", "spotify:now_playing", SAME),
    ("play Bad Guy on spotify", "spotify:play", SAME),
    ("play the album Thriller on Spotify", "spotify:play", SAME),
    # YouTube: "play/watch" opens on the laptop, "find/show/link" only finds
    ("play Bad Guy on youtube", "youtube+play", SAME),
    ("watch the iPhone 17 review on youtube", "youtube+play", SAME),
    ("play Bad Guy", "youtube+play", "llm"),
    ("find Bad Guy on youtube", "youtube", SAME),
    ("find a video of how to tie a tie on youtube", "youtube", SAME),
    ("show me lofi music on youtube", "youtube", SAME),
    ("send me a link to Bad Guy on youtube", "youtube", "llm"),
    ("how to fix a flat tire youtube", "youtube", "llm"),
    # News
    ("what's the latest news on Apple", "news", SAME),
    ("news about OpenAI", "news", SAME),
    ("brief me", "news", "llm"),
    ("top headlines", "news", "llm"),
    # General questions and weather
    ("who won the world series", "search", "llm"),
    ("what is the capital of France", "search", SAME),
    ("when does Bruin Plate open", "search", SAME),
    ("look up the population of Japan", "search", SAME),
    ("coffee shops near me", "search", "llm"),
    ("what's the weather in Los Angeles", "search", SAME),
    ("weather tomorrow", "search", "llm"),
    ("will it rain today", "search", "llm"),
    ("is it going to rain tomorrow", "search", "llm"),
    ("rain jacket", SHOP, "llm"),  # a product, not the weather
    ("tell me about the iPhone", "search", "llm"),
    # Messaging: capitalized names
    ("text Legend I'm late", "message", SAME),
    ("text Jonathan hi", "message", SAME),
    ("message Alex see you soon", "message", SAME),
    ("tell Mom I'm on my way", "message", SAME),
    # Messaging: explicit forms with a lowercase name
    ("send a message to jonathan", "message", SAME),
    ("send jonathan a message", "message", SAME),
    ('text jonathan "lol"', "message", SAME),
    ("tell jonathan that I'm late", "message", SAME),
    ("message alex saying hi", "message", SAME),
    ("imessage legend hi", "message", SAME),
    # Messaging: a lowercase name followed by obvious message content
    ("text legend i'm late", "message", SAME),
    ("text legend im on my way", "message", SAME),
    ("text legend omw", "message", SAME),
    ("text legend running late", "message", SAME),
    ("text legend, i'm late", "message", SAME),
    ("message alex see you soon", "message", SAME),
    ("text mom happy birthday", "message", SAME),
    ("text jonathan call me", "message", SAME),
    ("text sarah where are you", "message", SAME),
    ("tell legend i'm late", "message", SAME),
    # Not messaging, although they start with a messaging verb
    ("text size bigger", SHOP, "llm"),
    ("make the text size bigger", SHOP, "llm"),
    ("message length limit", SHOP, "llm"),
    ("text scale review", "reviews", "llm"),
    ("text me the price of AirPods", "price+reviews", "llm"),
    # Small talk: the LLM answers; without one the rules answer without any agent
    ("hi", "none", "llm"),
    ("hello", "none", "llm"),
    ("thanks", "none", "llm"),
    ("good morning", "none", "llm"),
    ("how are you", "none", "llm"),
    ("what can you do", "none", "llm"),
    ("who are you", "none", "llm"),
    ("what's up", "none", "llm"),
]


@pytest.fixture
def route(monkeypatch):
    seen = []

    async def call_agent(kind, args, config, http, emit=None):
        seen.append(f"spotify:{args.action}" if kind.value == "spotify" else kind.value)
        return AgentCall(kind=kind, payload={}, result={"summary": "ok"}, ms=0)

    async def prepare_message(text, config, http, emit=None):
        seen.append("message")
        return AgentCall(kind="message", payload={}, result={"summary": "ok"}, ms=0)

    async def consider(text, config, http, emit=None):
        seen.append("consider")
        return {"summary": "ok", "product": "x"}

    async def buyer_call(payload, config, http, emit=None, confirmation=None):
        seen.append("buyer")
        return AgentCall(kind="buyer", payload={}, result={"summary": "ok"}, ms=0)

    async def play_on_laptop(call, config, http, emit=None):
        seen.append("play")
        return call

    async def chat_with_llm(history, config, http, emit=None, preferences=None):
        seen.append("llm")
        return ChatReply(reply="ok", calls=[], source="asi_one")

    for name, fake in [
        ("call_agent", call_agent),
        ("prepare_message", prepare_message),
        ("consider", consider),
        ("buyer_call", buyer_call),
        ("play_on_laptop", play_on_laptop),
        ("chat_with_llm", chat_with_llm),
    ]:
        monkeypatch.setattr(chat_module, name, fake)

    def refuse(request):
        raise AssertionError(f"unexpected network call to {request.url}")

    async def run(text, asi_key):
        seen.clear()
        config = Config(
            _env_file=None,
            supabase_url="https://supabase.example",
            supabase_publishable_key="public-test",
            supabase_secret_key="secret-test",
            asi_one_api_key=asi_key,
        )
        async with httpx.AsyncClient(transport=httpx.MockTransport(refuse)) as http:
            await chat_module.chat([ChatMessage(role="user", content=text)], config, http)
        return "+".join(seen) or "none"

    return run


@pytest.mark.parametrize("text, keyword, _", MATRIX, ids=[row[0] for row in MATRIX])
async def test_keyword_mode_routes(route, text, keyword, _):
    assert await route(text, "") == keyword


@pytest.mark.parametrize("text, keyword, llm", MATRIX, ids=[row[0] for row in MATRIX])
async def test_llm_key_mode_routes(route, text, keyword, llm):
    assert await route(text, "asi-test-key") == (keyword if llm is SAME else llm)


@pytest.mark.parametrize(
    "text",
    [
        "text legend i'm late",
        "text legend im on my way",
        "text legend omw",
        "text legend running late",
        "message alex see you soon",
        "text mom happy birthday",
        "text jonathan call me",
        "text sarah where are you",
        "tell legend i'll be there in 5",
        "text legend: be there soon",
    ],
)
def test_lowercase_name_with_message_content_is_a_message(text):
    assert _is_message_request(text)


@pytest.mark.parametrize(
    "text",
    [
        "text size bigger",
        "make the text size bigger",
        "tell me about the iPhone",
        "message length limit",
        "text scale review",
        "text me the price of AirPods",
        "tell me where are you from in spanish",
        "text it to me",
    ],
)
def test_messaging_words_without_a_recipient_are_not_messages(text):
    assert not _is_message_request(text)
