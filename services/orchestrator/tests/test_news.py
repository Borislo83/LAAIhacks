import json

import httpx
import pytest
from fastapi.testclient import TestClient
from test_orchestrator import USER, config

from laai_orchestrator import chat
from laai_orchestrator.chat import (
    call_agent,
    chat_with_llm,
    chat_with_rules,
    public_https,
    safe_news,
)
from laai_orchestrator.main import create_app
from laai_orchestrator.models import AgentKind, ChatMessage, ToolArgs

IMAGE = "https://images.example.com/storm.jpg"
ARTICLE = "https://news.example.com/2026/10/17/storm"


def source(url=ARTICLE, **extra):
    return {
        "title": "Storm hits coast",
        "url": url,
        "publisher": "Example News",
        "published_at": "2026-10-17T08:00:00Z",
        **extra,
    }


def news_result(**overrides):
    return {
        "query": "hurricane",
        "headline": "Hurricane makes landfall",
        "summary": "The storm reached the coast overnight, Example News reports.",
        "image_url": IMAGE,
        "image_alt": "Waves over a sea wall",
        "image_source_url": ARTICLE,
        "sources": [source()],
        "error": None,
        **overrides,
    }


def agents(result=None, status=200):
    seen = []

    def handle(request):
        if request.url.path == "/auth/v1/user":
            return httpx.Response(200, json={"id": str(USER)})
        seen.append((request.url.path, json.loads(request.content)))
        return httpx.Response(status, json=result or news_result())

    return httpx.AsyncClient(transport=httpx.MockTransport(handle)), seen


@pytest.mark.parametrize(
    "url",
    [
        "https://news.example.com/a",
        "https://bbc.co.uk/news/1",
        "https://8.8.8.8/x",
        "https://example.com:443/x",
    ],
)
def test_public_https_keeps_public_https_urls(url):
    assert public_https(url) == url


@pytest.mark.parametrize(
    "url",
    [
        "http://news.example.com/a",
        "javascript:alert(1)",
        "data:image/png;base64,AAAA",
        "//news.example.com/a",
        "https://localhost/a",
        "https://localhost./a",
        "https://intranet/a",
        "https://printer.local/a",
        "https://db.internal/a",
        "https://127.0.0.1/a",
        "https://10.0.0.5/a",
        "https://192.168.1.1/a",
        "https://169.254.169.254/latest/meta-data",
        "https://[::1]/a",
        "https://user:pw@news.example.com/a",
        "https://news.example.com:99999/a",
        "https://news.example.com/a b",
        "https://news.example.com/" + "a" * 2100,
        "",
        None,
        42,
    ],
)
def test_public_https_rejects_unsafe_urls(url):
    assert public_https(url) is None


def test_safe_news_drops_bad_urls_and_caps_lengths():
    sources = [source(f"https://n{i}.example.com/a") for i in range(11)]
    sources[1] = source("http://insecure.example.com/a")
    sources[2] = source("https://10.0.0.1/a")
    sources[3] = "not a source"
    result = safe_news(
        news_result(
            headline="H" * 500,
            summary="S" * 2000,
            image_url="https://localhost/evil.png",
            image_source_url=ARTICLE,
            sources=sources,
        )
    )
    assert len(result["headline"]) == 200 and len(result["summary"]) == 800
    assert result["image_url"] is None
    assert result["image_alt"] is None and result["image_source_url"] is None
    assert len(result["sources"]) == 8
    assert all(public_https(s["url"]) for s in result["sources"])
    assert "https://n1.example.com/a" not in [s["url"] for s in result["sources"]]


def test_safe_news_keeps_a_good_image_but_drops_a_bad_credit_link():
    result = safe_news(news_result(image_source_url="http://news.example.com/a", sources=None))
    assert result["image_url"] == IMAGE and result["image_alt"] == "Waves over a sea wall"
    assert result["image_source_url"] is None and result["sources"] == []


async def test_call_agent_posts_query_and_max_sources_and_sanitizes():
    http, seen = agents(news_result(image_url="http://images.example.com/x.jpg"))
    async with http:
        call = await call_agent(
            AgentKind.NEWS, ToolArgs(query="hurricane", max_sources=4), config(), http
        )
    assert seen == [("/news", {"query": "hurricane", "max_sources": 4})]
    assert call.result["image_url"] is None and call.result["sources"][0]["url"] == ARTICLE
    assert call.error is None


async def test_news_agent_error_is_reported(monkeypatch):
    monkeypatch.setattr(chat, "RETRY_DELAY", 0)
    http, seen = agents(status=502)
    async with http:
        call = await call_agent(AgentKind.NEWS, ToolArgs(query="x"), config(), http)
    assert call.result is None and call.error and len(seen) == chat.ATTEMPTS


async def test_news_agent_error_field_is_kept_and_retried(monkeypatch):
    monkeypatch.setattr(chat, "RETRY_DELAY", 0)
    http, seen = agents(news_result(error="Search failed", sources=[]))
    async with http:
        call = await call_agent(AgentKind.NEWS, ToolArgs(query="x"), config(), http)
    assert call.error == "Search failed" and len(seen) == chat.ATTEMPTS


def test_direct_news_routes_validate_arguments():
    http, seen = agents()
    with TestClient(create_app(config(), http)) as client:
        headers = {"Authorization": "Bearer valid"}
        ok = client.post("/v1/agents/news", json={"query": "mars rover"}, headers=headers)
        assert ok.status_code == 200 and ok.json()["result"]["image_url"] == IMAGE
        assert seen[-1] == ("/news", {"query": "mars rover", "max_sources": 6})
        stream = client.post("/v1/agents/news/stream", json={"query": "mars"}, headers=headers)
        assert stream.status_code == 200 and "Hurricane makes landfall" in stream.text
        for bad in ({"product": "x"}, {"query": "x", "max_sources": 2}, {"query": "x" * 301}):
            assert client.post("/v1/agents/news", json=bad, headers=headers).status_code == 422
    assert [path for path, _ in seen if path == "/news"] == ["/news", "/news"]


@pytest.mark.parametrize(
    "text",
    [
        "news about the hurricane",
        "top headlines today",
        "what's happening with the Dodgers",
        "what is going on in Gaza",
        "latest on the Mars rover",
        "what happened at the debate",
    ],
)
async def test_rules_route_news_requests_to_news_only(text):
    http, seen = agents()
    async with http:
        reply = await chat_with_rules([ChatMessage(role="user", content=text)], config(), http)
    assert seen == [("/news", {"query": text, "max_sources": 6})]
    assert reply.calls[0].kind == AgentKind.NEWS
    assert reply.reply.startswith("Hurricane makes landfall.")


@pytest.mark.parametrize(
    "text",
    [
        "latest on iPhone 16 prices",
        "headphones under $100",
        "compare AirPods and Galaxy Buds",
        "alternatives to the Kindle news edition",
        "is the Pixel 9 worth it",
        "sony wh-1000xm5 reviews",
    ],
)
async def test_rules_keep_shopping_requests_off_news(text):
    http, seen = agents({"summary": "ok"})
    async with http:
        await chat_with_rules([ChatMessage(role="user", content=text)], config(), http)
    paths = [path for path, _ in seen]
    assert paths and "/news" not in paths and "/youtube" not in paths


@pytest.mark.parametrize(
    "text",
    ["play never gonna give you up", "youtube news clips about the eclipse", "find the song hello"],
)
async def test_rules_keep_media_requests_on_youtube(text):
    http, seen = agents({"summary": "ok", "url": None, "alternatives": []})
    async with http:
        await chat_with_rules([ChatMessage(role="user", content=text)], config(), http)
    assert [path for path, _ in seen] == ["/youtube"]


async def test_full_chat_routes_shopping_budget_and_news_without_llm():
    """chat() sends budgets to the coordinator and news to the news agent."""
    http, seen = agents()
    async with http:
        await chat.chat([ChatMessage(role="user", content="what happened at SpaceX")], config(), http)
        await chat.chat([ChatMessage(role="user", content="news on TVs under $500")], config(), http)
    assert [path for path, _ in seen] == ["/news", "/consider"]


async def test_llm_uses_the_news_tool():
    llm = []

    def handle(request):
        if request.url.host == "api.asi1.ai":
            body = json.loads(request.content)
            llm.append(body)
            if len(llm) == 1:
                call = {
                    "id": "t1",
                    "type": "function",
                    "function": {
                        "name": "summarize_news",
                        "arguments": json.dumps({"query": "hurricane", "max_sources": 5}),
                    },
                }
                return httpx.Response(200, json={"choices": [{"message": {"tool_calls": [call]}}]})
            return httpx.Response(200, json={"choices": [{"message": {"content": "A storm hit."}}]})
        assert request.url.path == "/news"
        assert json.loads(request.content) == {"query": "hurricane", "max_sources": 5}
        return httpx.Response(
            200, json=news_result(sources=[source(), source("https://192.168.0.1/x")])
        )

    async with httpx.AsyncClient(transport=httpx.MockTransport(handle)) as http:
        reply = await chat_with_llm(
            [ChatMessage(role="user", content="what's the latest on the hurricane")],
            config(asi_one_api_key="key"),
            http,
        )
    assert reply.reply == "A storm hit."
    assert reply.calls[0].kind == AgentKind.NEWS
    assert [s["url"] for s in reply.calls[0].result["sources"]] == [ARTICLE]
    tool = next(t for t in llm[0]["tools"] if t["function"]["name"] == "summarize_news")
    assert "headline" in tool["function"]["description"]
    assert "summarize_news" in llm[0]["messages"][0]["content"]
    # The LLM sees only the sanitized result.
    assert "192.168.0.1" not in llm[1]["messages"][-1]["content"]


async def test_llm_bad_news_arguments_are_rejected():
    llm = []

    def handle(request):
        assert request.url.host == "api.asi1.ai", "no agent call for bad arguments"
        llm.append(json.loads(request.content))
        if len(llm) == 1:
            call = {
                "id": "t1",
                "type": "function",
                "function": {"name": "summarize_news", "arguments": '{"query": "x", "max_sources": 50}'},
            }
            return httpx.Response(200, json={"choices": [{"message": {"tool_calls": [call]}}]})
        return httpx.Response(200, json={"choices": [{"message": {"content": "Sorry."}}]})

    async with httpx.AsyncClient(transport=httpx.MockTransport(handle)) as http:
        reply = await chat_with_llm(
            [ChatMessage(role="user", content="news")], config(asi_one_api_key="key"), http
        )
    assert reply.calls == [] and "bad arguments" in llm[1]["messages"][-1]["content"]
