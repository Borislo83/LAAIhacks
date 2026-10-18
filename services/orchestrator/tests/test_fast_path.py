"""Clear commands skip the ASI:One routing round-trip; ambiguous ones still use it."""

import json

import httpx
import pytest

from laai_orchestrator.chat import chat, fast_route
from laai_orchestrator.config import Config
from laai_orchestrator.models import AgentKind, ChatMessage

SPOTIFY_TOKEN = "spotify-test-token"
LAPTOP_TOKEN = "laptop-test-token"
WATCH = "https://www.youtube.com/watch?v=dQw4w9WgXcQ"


def config(**overrides):
    return Config(
        _env_file=None,
        **{
            "supabase_url": "https://supabase.example",
            "supabase_publishable_key": "public-test",
            "supabase_secret_key": "secret-test",
            "asi_one_api_key": "asi-test-key",
            **overrides,
        },
    )


def backend(youtube=None, laptop=None):
    seen = []

    def handle(request):
        body = json.loads(request.content) if request.content else {}
        seen.append((request.url.host, request.url.path.strip("/"), body))
        if request.url.host == "api.asi1.ai":
            return httpx.Response(200, json={"choices": [{"message": {"content": "Hi! I can help."}}]})
        path = request.url.path.strip("/")
        if path == "youtube":
            return httpx.Response(200, json=youtube or {"summary": "Found it.", "url": WATCH, "title": "Song"})
        if path == "youtube/open":
            return httpx.Response(200, json=laptop or {"status": "opened", "summary": "Opened."})
        if path == "spotify":
            return httpx.Response(200, json={"action": body.get("action"), "summary": "Paused Spotify."})
        if path == "imessage/prepare":
            return httpx.Response(200, json={"summary": "Draft ready for review.", "draft_id": "d1", "text": "hi"})
        return httpx.Response(200, json={"summary": f"{path} for {body.get('product') or body.get('query')}"})

    return httpx.AsyncClient(transport=httpx.MockTransport(handle)), seen


def user(text):
    return [ChatMessage(role="user", content=text)]


@pytest.mark.parametrize(
    "text, expected",
    [
        ("compare prices for Sony WH-1000XM5", [("price", "Sony WH-1000XM5")]),
        ("Price of the AirPods Pro 2?", [("price", "AirPods Pro 2")]),
        ("how much is the Nintendo Switch 2", [("price", "Nintendo Switch 2")]),
        ("hey, where can I buy a Garmin Forerunner 265", [("price", "Garmin Forerunner 265")]),
        ("compare Sony WH-1000XM5 with rivals",
         [("price", "Sony WH-1000XM5"), ("alternatives", "Sony WH-1000XM5")]),
        ("cheaper alternatives to Sony WH-1000XM5",
         [("price", "Sony WH-1000XM5"), ("alternatives", "Sony WH-1000XM5")]),
        ("compare Sony WH-1000XM5 vs Bose QuietComfort Ultra",
         [("price", "Sony WH-1000XM5"), ("price", "Bose QuietComfort Ultra")]),
    ],
)
def test_clear_shopping_commands_take_the_fast_path(text, expected):
    route = fast_route(text)
    assert [(kind.value, args.product) for kind, args in route] == expected
    # Grounded by construction: no store, price or budget the user didn't say.
    assert all(
        args.current_store is None and args.current_price is None and args.max_price is None
        for _, args in route
    )


@pytest.mark.parametrize(
    "text, kind",
    [
        ("pause", AgentKind.SPOTIFY),
        ("skip", AgentKind.SPOTIFY),
        ("play Blinding Lights on Spotify", AgentKind.SPOTIFY),
        ("what's playing", AgentKind.SPOTIFY),
        ("play Bad Guy on YouTube", AgentKind.YOUTUBE),
        ("latest news on the Fed", AgentKind.NEWS),
        ("what's the latest on SpaceX", AgentKind.NEWS),
    ],
)
def test_clear_media_and_news_commands_take_the_fast_path(text, kind):
    [(routed, _)] = fast_route(text)
    assert routed == kind


@pytest.mark.parametrize(
    "text",
    [
        "Is the Sony WH-1000XM5 worth it?",
        "I'm on Best Buy looking at AirPods Pro 2. Cheaper anywhere?",
        "compare prices for Sony WH-1000XM5 at Amazon",  # a store constraint: coordinator
        "cheaper alternatives to Sony WH-1000XM5 under $300",  # a budget: coordinator
        "how much is it",
        "play Bad Guy",  # YouTube or Spotify? ASI:One decides
        "hello there",
        "what do you think about noise cancelling headphones for flights",
    ],
)
def test_ambiguous_or_constrained_requests_do_not_take_the_fast_path(text):
    assert fast_route(text) is None


async def test_fast_path_skips_asi_one_and_runs_agents_together():
    http, seen = backend()
    async with http:
        reply = await chat(user("compare Sony WH-1000XM5 with rivals"), config(), http)
    assert not any(host == "api.asi1.ai" for host, _, _ in seen)
    assert sorted(path for _, path, _ in seen) == ["alternatives", "price"]
    assert [c.kind for c in reply.calls] == ["price", "alternatives"]
    assert reply.source == "rules"
    assert reply.reply == "price for Sony WH-1000XM5 alternatives for Sony WH-1000XM5"


async def test_conversational_requests_still_go_to_asi_one():
    http, seen = backend()
    async with http:
        reply = await chat(user("What should I look for in travel headphones?"), config(), http)
    assert any(host == "api.asi1.ai" for host, _, _ in seen)
    assert reply.source == "asi_one"


async def test_fast_path_keeps_the_spotify_token_out_of_the_recorded_payload():
    http, seen = backend()
    async with http:
        reply = await chat(user("pause"), config(spotify_executor_token=SPOTIFY_TOKEN), http)
    [(_, _, body)] = [s for s in seen if s[1] == "spotify"]
    assert body["executor_token"] == SPOTIFY_TOKEN and body["action"] == "pause"
    assert "executor_token" not in reply.calls[0].payload
    assert not any(host == "api.asi1.ai" for host, _, _ in seen)


async def test_fast_path_says_playing_only_when_the_laptop_opened_it():
    http, _ = backend(laptop={"status": "failed", "summary": "Laptop asleep."})
    async with http:
        reply = await chat(user("play Bad Guy on YouTube"), config(laptop_executor_token=LAPTOP_TOKEN), http)
    assert "Playing" not in reply.reply and "Not opened on your laptop" in reply.reply
    http, _ = backend()
    async with http:
        reply = await chat(user("play Bad Guy on YouTube"), config(laptop_executor_token=LAPTOP_TOKEN), http)
    assert reply.reply == "Playing Song on your laptop."


async def test_text_requests_still_only_draft():
    http, seen = backend()
    async with http:
        reply = await chat(user("text Jonathan I'm running late"), config(), http)
    assert [path for _, path, _ in seen] == ["imessage/prepare"]
    assert reply.calls[0].kind == "message"
