"""General web search: routing (search vs news vs other agents), sanitizing, LLM tool."""

import json

import httpx
import pytest
from test_orchestrator import config

from laai_orchestrator import chat
from laai_orchestrator.chat import (
    call_agent,
    chat_with_llm,
    chat_with_rules,
    fast_route,
    safe_search,
    search_ask,
)
from laai_orchestrator.models import AgentKind, ChatMessage, ToolArgs

PAGE = "https://dining.ucla.edu/bruin-plate/"


def search_result(**overrides):
    return {
        "query": "when does Bruin Plate open",
        "headline": "When does Bruin Plate open?",
        "title": "When does Bruin Plate open?",
        "summary": "Bruin Plate opens at 7:00 a.m. on weekdays.",
        "image_url": None,
        "sources": [{"title": "Bruin Plate", "url": PAGE, "publisher": "dining.ucla.edu"}],
        "error": None,
        "partial": False,
        "news": False,
        "data_source": "web",
        "elapsed_ms": 2100,
        **overrides,
    }


def agents(result=None):
    seen = []

    def handle(request):
        if request.url.host == "api.asi1.ai":
            return httpx.Response(200, json={"choices": [{"message": {"content": "Hi!"}}]})
        path = request.url.path.strip("/")
        body = json.loads(request.content)
        seen.append((path, body))
        if path == "search":
            return httpx.Response(200, json=result or search_result())
        if path == "news":
            return httpx.Response(200, json={"summary": "News.", "headline": "Story", "sources": []})
        if path == "spotify":
            return httpx.Response(200, json={"action": body.get("action"), "summary": "Paused."})
        if path == "imessage/prepare":
            return httpx.Response(200, json={"summary": "Draft ready.", "draft_id": "d1", "text": "hi"})
        return httpx.Response(200, json={"summary": f"{path} ok", "url": None, "alternatives": []})

    return httpx.AsyncClient(transport=httpx.MockTransport(handle)), seen


def user(text):
    return [ChatMessage(role="user", content=text)]


SEARCH_PHRASES = [
    "when does Bruin Plate open",
    "what is the Fetch.ai uAgents framework",
    "who is the chancellor of UCLA",
    "how do I renew a California driver's license",
    "hey, look up the Bruin Plate hours",
    "search for the best hiking trails in Malibu",
    "weather in Los Angeles today",
    "what's the weather in LA",
    "google the population of Tokyo",
]


@pytest.mark.parametrize("text", SEARCH_PHRASES)
def test_clear_questions_take_the_search_fast_path(text):
    [(kind, args)] = fast_route(text)
    assert kind == AgentKind.SEARCH and args.query == text


@pytest.mark.parametrize(
    "text, kind",
    [
        ("latest news on the Fed", AgentKind.NEWS),
        ("what's the latest on SpaceX", AgentKind.NEWS),
        ("pause", AgentKind.SPOTIFY),
        ("what's playing", AgentKind.SPOTIFY),
        ("play Bad Guy on YouTube", AgentKind.YOUTUBE),
        ("compare prices for Sony WH-1000XM5", AgentKind.PRICE),
        ("how much is the Nintendo Switch 2", AgentKind.PRICE),
    ],
)
def test_other_agents_keep_their_requests(text, kind):
    route = fast_route(text)
    assert route and route[0][0] == kind


@pytest.mark.parametrize(
    "text",
    [
        "what is the price of the AirPods Pro 2",  # shopping
        "what are the best deals on TVs",  # shopping
        "is the Pixel 9 worth it",
        "what is going on in Gaza",  # news phrasing: news or ASI:One, never web search
        "what's happening with the Dodgers",
        "how do I play guitar",  # media word: ASI:One decides
        "what's up",  # small talk
        "who are you",
        "how can you help me",
        "what is it",  # a pronoun, not a topic
        "what should I look for in travel headphones",
        "hello there",
    ],
)
def test_search_does_not_steal_other_requests(text):
    route = fast_route(text)
    assert not route or route[0][0] != AgentKind.SEARCH


async def test_fast_search_skips_asi_one_and_names_sources():
    http, seen = agents()
    async with http:
        reply = await chat.chat(user("when does Bruin Plate open"), config(asi_one_api_key="k"), http)
    assert seen == [("search", {"query": "when does Bruin Plate open", "max_sources": 6})]
    assert reply.calls[0].kind == AgentKind.SEARCH and reply.source == "rules"
    assert reply.reply == "Bruin Plate opens at 7:00 a.m. on weekdays. (Sources: dining.ucla.edu)"


@pytest.mark.parametrize(
    "text, path",
    [
        ("when does Bruin Plate open", "search"),
        ("best ramen near UCLA", "search"),
        ("weather in Los Angeles", "search"),
        ("news about the hurricane", "news"),
        ("what is going on in Gaza", "news"),
        ("is the Pixel 9 worth it", "reviews"),
        ("play never gonna give you up", "youtube"),
    ],
)
async def test_rules_route_search_vs_news_vs_others(text, path):
    http, seen = agents()
    async with http:
        await chat_with_rules(user(text), config(), http)
    assert path in [p for p, _ in seen]
    if path != "search":
        assert "search" not in [p for p, _ in seen]


async def test_rules_keep_bare_products_on_shopping():
    http, seen = agents()
    async with http:
        await chat_with_rules(user("Sony WH-1000XM5"), config(), http)
    assert sorted(p for p, _ in seen) == ["alternatives", "price", "reviews"]


async def test_messages_still_draft_and_search_verbs_break_out_of_a_draft():
    assert not search_ask("text Jonathan what is the plan")
    http, seen = agents()
    async with http:
        reply = await chat.chat(user("text Jonathan what is the plan"), config(), http)
    assert [p for p, _ in seen] == ["imessage/prepare"] and reply.calls[0].kind == "message"
    history = [
        ChatMessage(role="user", content="text Jonathan"),
        ChatMessage(role="assistant", content="What would you like to say to Jonathan?"),
        ChatMessage(role="user", content="look up when Bruin Plate opens"),
    ]
    http, seen = agents()
    async with http:
        await chat.chat(history, config(), http)
    assert [p for p, _ in seen] == ["search"]


def test_safe_search_keeps_the_news_shape_and_caps_sources():
    sources = [{"title": f"t{i}", "url": f"https://site{i}.example.com/a", "publisher": f"p{i}"}
               for i in range(6)]  # fmt: skip
    sources.insert(0, {"title": "local", "url": "http://127.0.0.1/admin", "publisher": "x"})
    clean = safe_search(
        search_result(
            sources=sources,
            summary="s" * 5000,
            image_url="https://10.0.0.1/x.jpg",
            injected="<script>",
            elapsed_ms=True,
        )
    )
    assert [s["url"] for s in clean["sources"]] == [f"https://site{i}.example.com/a" for i in range(3)]
    assert len(clean["summary"]) == chat.NEWS_SUMMARY_MAX
    assert clean["image_url"] is None and "injected" not in clean
    assert clean["headline"] == "When does Bruin Plate open?" and clean["elapsed_ms"] is None
    assert set(clean) >= {"headline", "title", "summary", "sources", "image_url", "error", "partial"}


async def test_call_agent_posts_to_search_and_sanitizes():
    http, seen = agents(search_result(sources=[{"url": "https://192.168.1.1/x", "title": "x"}]))
    async with http:
        call = await call_agent(AgentKind.SEARCH, ToolArgs(query="what is uAgents"), config(), http)
    assert seen == [("search", {"query": "what is uAgents", "max_sources": 6})]
    assert call.result["sources"] == [] and call.error is None


async def test_llm_uses_the_web_search_tool():
    llm = []

    def handle(request):
        if request.url.host == "api.asi1.ai":
            llm.append(json.loads(request.content))
            if len(llm) == 1:
                call = {
                    "id": "t1",
                    "type": "function",
                    "function": {"name": "web_search", "arguments": json.dumps({"query": "best ramen near UCLA"})},
                }
                return httpx.Response(200, json={"choices": [{"message": {"tool_calls": [call]}}]})
            return httpx.Response(200, json={"choices": [{"message": {"content": "Try Tsujita."}}]})
        assert request.url.path == "/search"
        return httpx.Response(200, json=search_result())

    async with httpx.AsyncClient(transport=httpx.MockTransport(handle)) as http:
        reply = await chat_with_llm(user("any good ramen around here?"), config(asi_one_api_key="k"), http)
    assert reply.calls[0].kind == AgentKind.SEARCH and reply.reply == "Try Tsujita."
    names = [t["function"]["name"] for t in llm[0]["tools"]]
    assert "web_search" in names and "summarize_news" in names
    assert "web_search" in llm[0]["messages"][0]["content"]
