import asyncio
import json

import httpx
from fastapi.testclient import TestClient
from test_orchestrator import USER, config

from laai_orchestrator.main import create_app
from laai_orchestrator.models import ChatReply
from laai_orchestrator.progress import event_stream


async def test_progress_is_yielded_before_work_finishes_and_disconnect_cancels():
    release, cancelled = asyncio.Event(), asyncio.Event()

    async def work(emit):
        emit({"type": "progress", "kind": "price", "stage": "searching", "message": "Searching"})
        try:
            await release.wait()
        finally:
            cancelled.set()
        return ChatReply(reply="Done", calls=[], source="rules")

    stream = event_stream(work)
    first = await asyncio.wait_for(anext(stream), timeout=1)
    assert '"stage": "searching"' in first and not release.is_set()
    await stream.aclose()
    assert cancelled.is_set()


def test_authenticated_stream_relays_agent_progress_and_terminal_result():
    seen = []

    async def handle(request):
        if request.url.path == "/auth/v1/user":
            assert request.headers["authorization"] == "Bearer valid"
            return httpx.Response(200, json={"id": str(USER)})
        body = json.loads(request.content)
        seen.append(request.url.path)
        if request.url.path == "/progress":
            return httpx.Response(
                200,
                json={
                    "events": [
                        {
                            "seq": 1,
                            "kind": "price",
                            "stage": "searching",
                            "message": "Searching online",
                        },
                        {
                            "seq": 2,
                            "kind": "price",
                            "stage": "validating",
                            "message": "Checking sources",
                        },
                    ]
                    if body["after"] == 0
                    else []
                },
            )
        assert request.url.path == "/price" and len(body["progress_id"]) == 32
        await asyncio.sleep(0.02)
        return httpx.Response(200, json={"summary": "No verified offers", "offers": []})

    client_http = httpx.AsyncClient(transport=httpx.MockTransport(handle))
    with TestClient(create_app(config(chat_auth_required=False), client_http)) as client:
        response = client.post("/v1/agents/price/stream", json={"product": "Sony XM5"},
                               headers={"Authorization": "Bearer valid"})
    events = [
        json.loads(line[6:]) for line in response.text.splitlines() if line.startswith("data: ")
    ]
    assert response.headers["content-type"].startswith("text/event-stream")
    assert [e["stage"] for e in events if e.get("seq")] == ["searching", "validating"]
    assert events[-1]["type"] == "result" and events[-1]["data"]["kind"] == "price"
    assert seen.count("/price") == 1


def test_stream_auth_matches_json_endpoint():
    # The old JSON-chat prototype bypass must never open dashboard streams.
    with TestClient(create_app(config(chat_auth_required=False))) as client:
        for path, body in [
            ("/v1/chat/stream", {"messages": [{"role": "user", "content": "Hi"}]}),
            ("/v1/agents/price/stream", {"product": "Sony XM5"}),
            ("/v1/buyer/prepare/stream", {"product": "Sony XM5"}),
        ]:
            assert client.post(path, json=body).status_code == 401


def test_chat_confirmation_requires_explicit_confirmation_and_is_labeled_demo():
    seen = []

    def handle(request):
        seen.append(request.url.path)
        assert request.url.path == "/buy/confirm"
        return httpx.Response(
            200,
            json={
                "demo": True,
                "purchase_enabled": False,
                "status": "cart_added",
                "summary": "Item verified in Amazon cart — no order placed.",
            },
        )

    http = httpx.AsyncClient(transport=httpx.MockTransport(handle))
    with TestClient(create_app(config(chat_auth_required=False), http)) as client:
        response = client.post(
            "/v1/chat",
            json={
                "messages": [{"role": "user", "content": "yes, confirm buy"}],
                "purchase_confirmation": "a" * 32,
            },
        )
        assert response.json()["source"] == "buyer"
        assert response.json()["calls"][0]["result"]["status"] == "cart_added"
    assert seen == ["/buy/confirm"]


async def test_progress_emitted_while_a_drain_is_in_flight_is_not_lost():
    from laai_orchestrator.progress import agent_request

    progress, done = [], asyncio.Event()

    async def handle(request):
        body = json.loads(request.content)
        if request.url.path == "/progress":
            snapshot = [e for e in progress if e["seq"] > body["after"]]
            done.set()  # The operation finishes while this poll is still answering.
            await asyncio.sleep(0.05)
            return httpx.Response(200, json={"events": snapshot})
        await done.wait()
        progress.append({"seq": 1, "kind": "price", "stage": "validating", "message": "Last"})
        return httpx.Response(200, json={"summary": "Done"})

    seen = []
    async with httpx.AsyncClient(transport=httpx.MockTransport(handle)) as http:
        response = await agent_request(
            "price", {"product": "X"}, config(), http, seen.append, timeout=5
        )
    assert response.json() == {"summary": "Done"}
    assert [e["stage"] for e in seen] == ["validating"]


async def test_llm_tool_calls_run_concurrently_and_keep_their_order():
    from laai_orchestrator.chat import chat_with_llm
    from laai_orchestrator.models import ChatMessage

    llm_requests, in_flight, peak = [], [0], [0]

    def tool_call(call_id, name, arguments):
        return {"id": call_id, "type": "function",
                "function": {"name": name, "arguments": json.dumps(arguments)}}

    async def handle(request):
        body = json.loads(request.content)
        if request.url.host == "api.asi1.ai":
            llm_requests.append(body)
            if len(llm_requests) == 1:
                calls = [
                    tool_call("t1", "compare_prices", {"product": "Sony XM5"}),
                    tool_call("t2", "nope", {"product": "Sony XM5"}),
                    tool_call("t3", "get_reviews", {"product": "Sony XM5"}),
                ]
                return httpx.Response(200, json={"choices": [{"message": {"tool_calls": calls}}]})
            return httpx.Response(200, json={"choices": [{"message": {"content": "Answer"}}]})
        in_flight[0] += 1
        peak[0] = max(peak[0], in_flight[0])
        # Price is slower, so finishing order differs from the requested order.
        await asyncio.sleep(0.1 if request.url.path == "/price" else 0.02)
        in_flight[0] -= 1
        return httpx.Response(200, json={"summary": request.url.path})

    async with httpx.AsyncClient(transport=httpx.MockTransport(handle)) as http:
        reply = await chat_with_llm(
            [ChatMessage(role="user", content="Is the Sony XM5 worth it?")],
            config(asi_one_api_key="key"),
            http,
        )
    assert reply.reply == "Answer" and peak[0] == 2
    assert [c.kind.value for c in reply.calls] == ["price", "reviews"]
    tools = [m for m in llm_requests[1]["messages"] if m["role"] == "tool"]
    assert [m["tool_call_id"] for m in tools] == ["t1", "t2", "t3"]
    assert "Unknown tool" in tools[1]["content"]
    assert json.loads(tools[2]["content"]) == {"summary": "/reviews"}
