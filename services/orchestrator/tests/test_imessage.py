import json

import httpx
import pytest
from fastapi.testclient import TestClient
from test_orchestrator import USER, config

from laai_orchestrator import main
from laai_orchestrator.chat import chat, chat_with_llm, chat_with_rules, prepare_message
from laai_orchestrator.main import create_app
from laai_orchestrator.models import ChatMessage

TOKEN = "executor-secret-never-returned"
DRAFT = {
    "draft_id": "d-123",
    "recipient_name": "Alex Kim",
    "recipient_masked": "A*** K**",
    "text": "Running 10 minutes late",
    "expires_at": "2026-10-17T21:00:00Z",
    "summary": "Draft to A*** K**: Running 10 minutes late. Review and confirm to send.",
    "error": None,
}


def agents(prepare=None, send=None, status=200, roles=("web",)):
    seen = []

    def handle(request):
        if request.url.path == "/auth/v1/user":
            if request.headers.get("authorization") != "Bearer valid":
                return httpx.Response(401, json={})
            return httpx.Response(
                200, json={"id": str(USER), "app_metadata": {"laai_roles": list(roles)}}
            )
        body = json.loads(request.content)
        seen.append((request.url.path, body))
        if request.url.path == "/imessage/prepare":
            return httpx.Response(status, json=prepare or DRAFT)
        if request.url.path == "/imessage/send":
            return httpx.Response(status, json=send or {"status": "sent", "summary": "Sent."})
        return httpx.Response(200, json={"summary": "Shopping answer"})

    return httpx.AsyncClient(transport=httpx.MockTransport(handle)), seen


@pytest.mark.parametrize(
    "text",
    [
        "text Alex I'm running 10 minutes late",
        "please message Sam that dinner is at 7",
        "tell Jordan I found the headphones",
        "send a text to Mom saying happy birthday",
        "use imessage to say hi to Pat",
    ],
)
async def test_rules_route_messaging_requests_to_prepare_only(text):
    http, seen = agents()
    async with http:
        reply = await chat_with_rules([ChatMessage(role="user", content=text)], config(), http)
    assert seen == [("/imessage/prepare", {"request": text})]
    assert reply.calls[0].kind == "message" and reply.calls[0].result["draft_id"] == "d-123"
    assert "Review and confirm" in reply.reply


@pytest.mark.parametrize("text", ["tell me about the Sony XM5", "text scale settings price"])
async def test_rules_do_not_route_other_requests_to_messaging(text):
    http, seen = agents(prepare={"summary": "x"})
    async with http:
        await chat_with_rules([ChatMessage(role="user", content=text)], config(), http)
    assert "/imessage/prepare" not in [path for path, _ in seen]


async def test_prepare_errors_and_bad_results_are_reported():
    for kwargs in ({"status": 500}, {"prepare": {"summary": 5}}):
        http, _ = agents(**kwargs)
        async with http:
            call = await prepare_message("text Alex hi", config(), http)
        assert call.result is None and call.error == "iMessage agent unavailable"


async def test_llm_can_draft_but_never_send():
    llm, sent_to_agents = [], []

    def handle(request):
        if request.url.host == "api.asi1.ai":
            llm.append(json.loads(request.content))
            if len(llm) == 1:
                call = {
                    "id": "t1",
                    "type": "function",
                    "function": {
                        "name": "message_friend",
                        "arguments": json.dumps({"request": "tell Alex I'm late"}),
                    },
                }
                return httpx.Response(200, json={"choices": [{"message": {"tool_calls": [call]}}]})
            return httpx.Response(200, json={"choices": [{"message": {"content": "Drafted."}}]})
        sent_to_agents.append(request.url.path)
        return httpx.Response(200, json=DRAFT)

    async with httpx.AsyncClient(transport=httpx.MockTransport(handle)) as http:
        reply = await chat_with_llm(
            [ChatMessage(role="user", content="tell Alex I'm late")],
            config(asi_one_api_key="key", imessage_executor_token=TOKEN),
            http,
        )
        # A "yes" afterwards must not send anything either.
        await chat(
            [ChatMessage(role="user", content="yes")],
            config(asi_one_api_key="key", imessage_executor_token=TOKEN),
            http,
        )
    assert "/imessage/send" not in sent_to_agents and "/imessage/prepare" in sent_to_agents
    assert reply.calls[0].kind == "message" and reply.calls[0].result["draft_id"] == "d-123"
    tool = next(m for m in llm[1]["messages"] if m["role"] == "tool")
    assert "d-123" not in tool["content"] and "Alex Kim" not in tool["content"]
    assert "message_friend" in [t["function"]["name"] for t in llm[0]["tools"]]
    assert not any("send" in t["function"]["name"] for t in llm[0]["tools"])
    assert TOKEN not in json.dumps(llm)


def send_client(token=TOKEN, **kwargs):
    http, seen = agents(**kwargs)
    app = create_app(config(chat_auth_required=False, imessage_executor_token=token), http)
    return TestClient(app), seen


BODY = {"draft_id": "d-123", "text": "Running 10 minutes late"}
AUTH = {"Authorization": "Bearer valid"}


def test_send_requires_sign_in_even_when_chat_auth_is_off():
    client, seen = send_client()
    with client:
        assert client.post("/v1/messages/send", json=BODY).status_code == 401
        assert client.post("/v1/messages/send", json=BODY,
                           headers={"Authorization": "Bearer wrong"}).status_code == 401
    assert seen == []


def test_send_requires_the_web_role():
    client, seen = send_client(roles=("g2",))
    with client:
        assert client.post("/v1/messages/send", json=BODY, headers=AUTH).status_code == 403
    assert seen == []


def test_send_is_unavailable_without_an_executor_token():
    client, seen = send_client(token="")
    with client:
        response = client.post("/v1/messages/send", json=BODY, headers=AUTH)
    assert response.status_code == 503
    assert response.json()["detail"] == "Messaging executor not configured"
    assert seen == []


def test_send_forwards_the_confirmed_draft_and_never_returns_the_token():
    client, seen = send_client(
        send={"status": "sent", "summary": "Sent to A*** K**.", "executor_token": TOKEN}
    )
    with client:
        response = client.post("/v1/messages/send", json=BODY, headers=AUTH)
    assert response.status_code == 200
    assert response.json() == {"status": "sent", "summary": "Sent to A*** K**.", "dry_run": False}
    assert TOKEN not in response.text
    assert seen == [("/imessage/send", {**BODY, "executor_token": TOKEN})]


def test_send_passes_dry_run_through():
    client, _ = send_client(send={"status": "sent", "dry_run": True, "summary": "Dry run only."})
    with client:
        response = client.post("/v1/messages/send", json=BODY, headers=AUTH)
    assert response.json() == {"status": "sent", "summary": "Dry run only.", "dry_run": True}


@pytest.mark.parametrize(
    "kwargs", [{"status": 500}, {"send": {"status": "maybe", "summary": "?"}}]
)
def test_send_agent_failures_do_not_leak_details(kwargs):
    client, _ = send_client(**kwargs)
    with client:
        response = client.post("/v1/messages/send", json=BODY, headers=AUTH)
    assert response.status_code == 502
    assert TOKEN not in response.text


def test_send_validates_its_body():
    client, seen = send_client()
    with client:
        for body in ({"draft_id": "d"}, {**BODY, "text": ""}, {**BODY, "extra": 1}):
            assert client.post("/v1/messages/send", json=body, headers=AUTH).status_code == 422
    assert seen == []


def test_send_is_rate_limited_per_user():
    client, seen = send_client()
    with client:
        codes = [
            client.post("/v1/messages/send", json=BODY, headers=AUTH).status_code
            for _ in range(main.MESSAGE_SENDS_PER_MINUTE + 1)
        ]
    assert codes == [200] * main.MESSAGE_SENDS_PER_MINUTE + [429]
    assert len(seen) == main.MESSAGE_SENDS_PER_MINUTE


def test_rate_limit_window_slides(monkeypatch):
    now = [0.0]
    monkeypatch.setattr(main, "monotonic", lambda: now[0])
    limit = main.RateLimit(2, 60)
    assert limit.allow(USER) and limit.allow(USER) and not limit.allow(USER)
    now[0] = 61
    assert limit.allow(USER)
