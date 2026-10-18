import json

import httpx
import pytest
from fastapi.testclient import TestClient
from test_orchestrator import USER, config

from laai_orchestrator import chat
from laai_orchestrator.chat import call_agent, chat_with_llm, chat_with_rules, safe_youtube
from laai_orchestrator.main import create_app
from laai_orchestrator.models import AgentKind, ChatMessage, ToolArgs

WATCH = "https://www.youtube.com/watch?v=dQw4w9WgXcQ"
SHORT = "https://youtu.be/dQw4w9WgXcQ"


def youtube_result(url=WATCH, alternatives=None, error=None):
    return {
        "query": "never gonna give you up",
        "kind": "song",
        "title": "Never Gonna Give You Up",
        "url": url,
        "channel": "Rick Astley",
        "duration": "3:33",
        "alternatives": alternatives or [],
        "summary": "Found the official video.",
        "error": error,
    }


def agents(result=None, status=200):
    seen = []

    def handle(request):
        if request.url.path == "/auth/v1/user":
            return httpx.Response(200, json={"id": str(USER)})
        seen.append((request.url.path, json.loads(request.content)))
        return httpx.Response(status, json=result or youtube_result())

    return httpx.AsyncClient(transport=httpx.MockTransport(handle)), seen


@pytest.mark.parametrize(
    "url,kept",
    [
        (WATCH, WATCH),
        (SHORT, SHORT),
        ("http://www.youtube.com/watch?v=dQw4w9WgXcQ", None),
        ("https://www.youtube.com/watch?v=dQw4w9WgXcQ&list=x", None),
        ("https://www.youtube.com/watch?v=short", None),
        ("https://evil.example/watch?v=dQw4w9WgXcQ", None),
        ("https://youtube.com.evil.example/watch?v=dQw4w9WgXcQ", None),
        ("javascript:alert(1)", None),
        (None, None),
        (42, None),
    ],
)
def test_only_canonical_youtube_links_survive(url, kept):
    result = safe_youtube(
        youtube_result(url, alternatives=[{"title": "Alt", "url": url, "channel": "C"}])
    )
    assert result["url"] == kept
    assert [a["url"] for a in result["alternatives"]] == ([kept] if kept else [])


async def test_call_agent_posts_query_and_kind_and_drops_bad_urls():
    http, seen = agents(youtube_result("https://evil.example/x"))
    async with http:
        call = await call_agent(
            AgentKind.YOUTUBE, ToolArgs(query="never gonna give you up", kind="song"), config(), http
        )
    assert seen == [("/youtube", {"query": "never gonna give you up", "kind": "song"})]
    assert call.result["url"] is None and call.error is None


async def test_youtube_agent_error_is_reported(monkeypatch):
    monkeypatch.setattr(chat, "RETRY_DELAY", 0)
    http, seen = agents(status=500)
    async with http:
        call = await call_agent(AgentKind.YOUTUBE, ToolArgs(query="x"), config(), http)
    assert call.result is None and call.error and len(seen) == chat.ATTEMPTS


def test_direct_youtube_route_requires_a_query():
    http, seen = agents()
    with TestClient(create_app(config(), http)) as client:
        headers = {"Authorization": "Bearer valid"}
        ok = client.post("/v1/agents/youtube", json={"query": "lofi beats"}, headers=headers)
        assert ok.status_code == 200 and ok.json()["result"]["url"] == WATCH
        assert seen[-1][1] == {"query": "lofi beats", "kind": "auto"}
        missing = client.post("/v1/agents/youtube", json={"product": "x"}, headers=headers)
        assert missing.status_code == 422
        assert client.post("/v1/agents/price", json={"query": "x"}, headers=headers).status_code == 422
    assert len(seen) == 1


@pytest.mark.parametrize(
    "text,kind",
    [
        ("play never gonna give you up", "auto"),
        ("find the song bohemian rhapsody", "song"),
        ("youtube video of the moon landing", "video"),
    ],
)
async def test_rules_route_media_requests_to_youtube_only(text, kind):
    http, seen = agents()
    async with http:
        reply = await chat_with_rules([ChatMessage(role="user", content=text)], config(), http)
    assert [path for path, _ in seen] == ["/youtube"]
    assert seen[0][1] == {"query": text, "kind": kind}
    assert reply.calls[0].kind == AgentKind.YOUTUBE and WATCH in reply.reply


@pytest.mark.parametrize(
    "text", ["cheapest price for a video game", "video doorbell reviews", "is this music speaker good"]
)
async def test_rules_keep_shopping_requests_on_shopping_agents(text):
    http, seen = agents({"summary": "ok"})
    async with http:
        await chat_with_rules([ChatMessage(role="user", content=text)], config(), http)
    assert seen and "/youtube" not in [path for path, _ in seen]


async def test_llm_uses_the_youtube_tool():
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
                        "name": "find_youtube",
                        "arguments": json.dumps({"query": "bohemian rhapsody", "kind": "song"}),
                    },
                }
                return httpx.Response(200, json={"choices": [{"message": {"tool_calls": [call]}}]})
            return httpx.Response(200, json={"choices": [{"message": {"content": "Here it is"}}]})
        assert request.url.path == "/youtube"
        assert json.loads(request.content) == {"query": "bohemian rhapsody", "kind": "song"}
        return httpx.Response(200, json=youtube_result(SHORT))

    async with httpx.AsyncClient(transport=httpx.MockTransport(handle)) as http:
        reply = await chat_with_llm(
            [ChatMessage(role="user", content="play bohemian rhapsody")],
            config(asi_one_api_key="key"),
            http,
        )
    assert reply.calls[0].kind == AgentKind.YOUTUBE and reply.calls[0].result["url"] == SHORT
    assert "find_youtube" in [t["function"]["name"] for t in llm[0]["tools"]]
    assert "YouTube" in llm[0]["messages"][0]["content"]
