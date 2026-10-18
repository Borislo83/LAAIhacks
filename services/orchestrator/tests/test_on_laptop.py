"""From the glasses, naming YouTube or Spotify, or adding to the Amazon cart, runs on the laptop."""

import asyncio

import pytest

from laai_orchestrator import chat as chat_mod
from laai_orchestrator.chat import laptop_spotify, on_laptop
from laai_orchestrator.models import AgentCall, AgentKind


@pytest.mark.parametrize(
    ("text", "action", "query"),
    [
        ("put on some chill music on Spotify", "play", "chill music"),
        ("can you play the weeknd on spotify please", "play", "the weeknd"),
        ("spotify play blinding lights", "play", "blinding lights"),
        ("open spotify", "resume", None),
        ("pause spotify", "pause", None),
        ("skip this song on spotify", "next", None),
        ("what's playing on spotify", "now_playing", None),
    ],
)
def test_any_spotify_request_is_a_spotify_action(text, action, query):
    args = laptop_spotify(text)
    assert (args.action, args.query) == (action, query)


@pytest.fixture
def calls(monkeypatch):
    seen = []

    async def call_agent(kind, args, *_a, **_k):
        seen.append((kind, args))
        return AgentCall(kind=kind, payload={}, result={"url": "https://www.youtube.com/watch?v=abcdefghijk", "summary": "ok"}, ms=0)

    async def play_on_laptop(call, *_a, **_k):
        seen.append(("play", call.kind))
        return call

    async def cart_add_call(product, *_a, **_k):
        seen.append(("cart", product))
        return AgentCall(kind="buyer", payload={}, result={"status": "cart_added", "summary": "Added.", "purchase_enabled": False}, ms=0)

    monkeypatch.setattr(chat_mod, "call_agent", call_agent)
    monkeypatch.setattr(chat_mod, "play_on_laptop", play_on_laptop)
    monkeypatch.setattr(chat_mod, "cart_add_call", cart_add_call)
    return seen


def run(text):
    return asyncio.run(on_laptop(text, [], None, None))


def test_any_youtube_mention_plays_on_the_laptop(calls):
    for text in ["find a youtube review of the Apple Watch", "youtube starboy", "Starboy on YouTube"]:
        calls.clear()
        assert run(text)
        assert calls[0][0] == AgentKind.YOUTUBE and calls[1] == ("play", AgentKind.YOUTUBE), text


def test_a_youtube_link_request_is_not_forced(calls):
    assert run("send me the youtube link for espresso") is None


def test_spotify_mention_controls_spotify(calls):
    assert run("put on some chill music on Spotify")
    assert calls[0][0] == AgentKind.SPOTIFY and calls[0][1].query == "chill music"


def test_add_to_amazon_cart_adds_the_named_product(calls):
    reply = run("add AirPods Pro 3 to my Amazon cart")
    assert calls == [("cart", "AirPods Pro 3")] and reply.reply == "Added."


def test_other_requests_are_left_to_chat(calls):
    assert run("add it to my cart") is None
    assert run("compare prices for AirPods Pro 3") is None
    assert calls == []


def test_a_glasses_message_is_drafted_to_the_default_and_sent(monkeypatch):
    from types import SimpleNamespace

    from pydantic import SecretStr

    seen = {}

    async def prepare_message(request, config, http, emit=None, to_default=False):
        seen["to_default"] = to_default
        return AgentCall(kind="message", payload={}, ms=0,
                         result={"draft_id": "d" * 32, "recipient_name": "Legend Yang", "text": "On my way."})

    class Http:
        async def post(self, url, json, timeout):
            seen["sent"] = (url, json["draft_id"], json["text"])
            return SimpleNamespace(raise_for_status=lambda: None,
                                   json=lambda: {"status": "sent", "summary": "Sent your iMessage to Legend Yang."})

    monkeypatch.setattr(chat_mod, "prepare_message", prepare_message)
    config = SimpleNamespace(imessage_executor_token=SecretStr("t"), agents_url="http://agents")
    call = asyncio.run(chat_mod.message_from_glasses("text I'm on my way", config, Http()))
    assert seen["to_default"] is True
    assert seen["sent"] == ("http://agents/imessage/send", "d" * 32, "On my way.")
    assert call.result["status"] == "sent" and "draft_id" not in call.result and call.error is None


def test_an_unfinished_glasses_draft_is_not_sent(monkeypatch):
    from types import SimpleNamespace

    async def prepare_message(request, config, http, emit=None, to_default=False):
        return AgentCall(kind="message", payload={}, ms=0,
                         result={"recipient_name": "Legend Yang", "summary": "What would you like to say to Legend Yang?"})

    class Http:
        async def post(self, *a, **k):
            raise AssertionError("must not send")

    monkeypatch.setattr(chat_mod, "prepare_message", prepare_message)
    call = asyncio.run(chat_mod.message_from_glasses("text Legend", SimpleNamespace(), Http()))
    assert "draft_id" not in call.result
