import json
from datetime import UTC, datetime
from uuid import uuid4

import httpx
import pytest
from fastapi.testclient import TestClient

from laai_orchestrator.config import Config
from laai_orchestrator.main import create_app
from laai_orchestrator.models import PageContext
from laai_orchestrator.predictor import predict
from laai_orchestrator.store import Store

USER = uuid4()
OTHER_USER = uuid4()
PAGE = {"url": "https://shop.example/sony", "product": "Sony XM5", "current_price": 399}
DEFAULTS = {
    "enabled_agents": ["price", "alternatives", "reviews"],
    "require_confirmation": True,
    "projection_enabled": True,
    "text_scale": 1,
}


def config(**overrides):
    return Config(
        _env_file=None,
        **{
            "supabase_url": "https://supabase.example",
            "supabase_publishable_key": "public-test",
            "supabase_secret_key": "secret-test",
            **overrides,
        },
    )


class Backend:
    """HTTP fixture; PostgreSQL invariants are tested separately with PGlite."""

    def __init__(self):
        self.settings = DEFAULTS.copy()
        self.preferences = {"display_density": "comfortable", "shopping_priority": "balanced",
            "proactive_enabled": True, "notes": "", "updated_at": "2026-10-17T00:00:00Z"}
        self.rows = {}
        self.agent_calls = []
        self.agent_error = False
        self.storage_error = False
        self.calls = []

    def handle(self, request):
        self.calls.append(request)
        path = request.url.path
        body = json.loads(request.content) if request.content else {}
        if path == "/auth/v1/user":
            token = request.headers.get("authorization")
            if token == "Bearer valid":
                return httpx.Response(200, json={"id": str(USER)})
            if token == "Bearer other":
                return httpx.Response(200, json={"id": str(OTHER_USER)})
            return httpx.Response(401, json={})
        if self.storage_error and path.startswith("/rest/"):
            return httpx.Response(500, json={"message": "private storage details"})
        if path == "/rest/v1/rpc/laai_preferences":
            if body.get("p_preferences") is not None:
                self.preferences.update(body["p_preferences"])
            return httpx.Response(200, json=self.preferences)
        if path == "/rest/v1/rpc/laai_get_settings":
            return httpx.Response(200, json=self.settings)
        if path == "/rest/v1/laai_settings":
            assert "resolution=merge-duplicates" in request.headers["prefer"]
            self.settings = {k: v for k, v in body.items() if k != "user_id"}
            return httpx.Response(201, json=[body])
        if path == "/rest/v1/rpc/laai_create_context":
            context_id = str(uuid4())
            tasks = []
            for item in body["p_tasks"]:
                row = {
                    "id": str(uuid4()),
                    "context_id": context_id,
                    "user_id": body["p_user_id"],
                    "status": "predicted",
                    "created_at": datetime.now(UTC).isoformat(),
                    **item,
                }
                self.rows[row["id"]] = row
                tasks.append(row)
            return httpx.Response(
                200,
                json={
                    "context_id": context_id,
                    "prediction_source": body["p_source"],
                    "tasks": tasks,
                },
            )
        if path == "/rest/v1/laai_tasks":
            params = request.url.params
            rows = [r for r in self.rows.values() if f"eq.{r['user_id']}" == params["user_id"]]
            if "id" in params:
                rows = [r for r in rows if f"eq.{r['id']}" == params["id"]]
            return httpx.Response(200, json=rows)
        if path == "/rest/v1/rpc/laai_claim_task":
            row = self.rows.get(body["p_task_id"])
            if not row or row["user_id"] != body["p_user_id"]:
                return httpx.Response(404, json={"code": "PT404"})
            if row["kind"] not in self.settings["enabled_agents"] or (
                self.settings["require_confirmation"] and not body["p_confirmed"]
            ):
                return httpx.Response(403, json={"code": "PT403"})
            if row["status"] != "predicted":
                return httpx.Response(409, json={"code": "PT409"})
            row["status"] = "running"
            return httpx.Response(200, json=row)
        if path == "/rest/v1/rpc/laai_finish_task":
            row = self.rows[body["p_task_id"]]
            row.update(
                status="failed" if body["p_error"] else "succeeded",
                result=body["p_result"],
                error=body["p_error"],
            )
            return httpx.Response(200, json=row)
        if request.url.host == "127.0.0.1":
            self.agent_calls.append((path, body))
            if self.agent_error:
                return httpx.Response(502, json={"error": "private upstream details"})
            return httpx.Response(200, json={"summary": "Best price is $299", "offers": []})
        raise AssertionError(f"Unexpected URL: {request.url}")


@pytest.fixture
def api():
    backend = Backend()
    http = httpx.AsyncClient(transport=httpx.MockTransport(backend.handle))
    with TestClient(create_app(config(), http)) as client:
        client.headers["Authorization"] = "Bearer valid"
        yield client, backend


def task_id(client):
    response = client.post("/v1/contexts", json=PAGE)
    assert response.status_code == 201
    return response.json()["tasks"][0]["id"]


def test_dashboard_auth_config_exposes_only_public_credentials(api):
    client, _ = api
    client.headers.pop("Authorization")
    response = client.get("/v1/auth/config")
    assert response.status_code == 200
    assert response.json() == {
        "supabase_url": "https://supabase.example",
        "supabase_publishable_key": "public-test",
    }
    assert "secret-test" not in response.text


def test_dashboard_direct_calls_require_verified_session(api):
    client, backend = api
    client.headers.pop("Authorization")
    for headers in ({}, {"Authorization": "Bearer invalid"}):
        response = client.post("/v1/agents/price", json={"product": "Sony XM5"}, headers=headers)
        assert response.status_code == 401
    assert backend.agent_calls == []
    response = client.post(
        "/v1/agents/price", json={"product": "Sony XM5"},
        headers={"Authorization": "Bearer valid"},
    )
    assert response.status_code == 200
    assert response.json()["result"]["summary"] == "Best price is $299"
    assert len(backend.agent_calls) == 1


def test_prediction_dispatch_and_restart_history(api):
    client, backend = api
    task = task_id(client)
    result = client.post(f"/v1/tasks/{task}/dispatch", json={"confirmed": True})
    assert result.status_code == 200
    assert result.json()["status"] == "succeeded"
    assert backend.agent_calls == [
        (
            "/price",
            {
                "product": "Sony XM5",
                "current_store": None,
                "current_price": 399,
            },
        )
    ]
    # A new application instance uses the same persisted Supabase records.
    http = httpx.AsyncClient(transport=httpx.MockTransport(backend.handle))
    with TestClient(create_app(config(), http)) as restarted:
        response = restarted.get(f"/v1/tasks/{task}", headers={"Authorization": "Bearer valid"})
        assert response.json()["result"]["summary"] == "Best price is $299"


def test_confirmation_permission_revocation_and_duplicate_dispatch(api):
    client, backend = api
    task = task_id(client)
    assert client.post(f"/v1/tasks/{task}/dispatch", json={}).status_code == 403
    backend.settings["enabled_agents"] = []
    assert client.post(f"/v1/tasks/{task}/dispatch", json={"confirmed": True}).status_code == 403
    assert backend.agent_calls == []
    backend.settings["enabled_agents"] = ["price"]
    assert client.post(f"/v1/tasks/{task}/dispatch", json={"confirmed": True}).status_code == 200
    assert client.post(f"/v1/tasks/{task}/dispatch", json={"confirmed": True}).status_code == 409
    assert len(backend.agent_calls) == 1


def test_user_isolation_and_spoofed_identity(api):
    client, backend = api
    task = task_id(client)
    client.headers["Authorization"] = "Bearer other"
    assert client.get("/v1/tasks").json() == []
    assert client.get(f"/v1/tasks/{task}").status_code == 404
    assert client.post(f"/v1/tasks/{task}/dispatch", json={"confirmed": True}).status_code == 404
    assert client.post("/v1/contexts", json={**PAGE, "user_id": str(USER)}).status_code == 422
    assert backend.agent_calls == []


def test_auth_and_input_validation(api):
    client, _ = api
    assert client.get("/health").status_code == 200
    client.headers.pop("Authorization")
    assert client.get("/v1/settings").status_code == 401
    client.headers["Authorization"] = "Bearer invalid"
    assert client.get("/v1/settings").status_code == 401
    client.headers["Authorization"] = "Bearer valid"
    assert client.post("/v1/contexts", json={**PAGE, "current_price": -1}).status_code == 422
    assert client.get("/v1/tasks?limit=101").status_code == 422


def test_settings_and_non_shopping_context(api):
    client, _ = api
    settings = {**DEFAULTS, "enabled_agents": ["reviews"], "text_scale": 1.5}
    assert client.put("/v1/settings", json=settings).json() == settings
    assert client.get("/v1/settings").json() == settings
    tasks = client.post("/v1/contexts", json=PAGE).json()["tasks"]
    assert [t["kind"] for t in tasks] == ["reviews"]
    page = {"url": "https://books.example/chapter-1", "title": "Chapter one"}
    assert client.post("/v1/contexts", json=page).json()["tasks"] == []


def test_agent_failure_is_persisted(api):
    client, backend = api
    task = task_id(client)
    backend.agent_error = True
    response = client.post(f"/v1/tasks/{task}/dispatch", json={"confirmed": True})
    assert response.json()["status"] == "failed"
    assert "private" not in response.text
    assert client.get(f"/v1/tasks/{task}").json()["status"] == "failed"


def test_storage_failure_stops_execution(api):
    client, backend = api
    task = task_id(client)
    backend.storage_error = True
    response = client.post(f"/v1/tasks/{task}/dispatch", json={"confirmed": True})
    assert response.status_code == 503
    assert "private" not in response.text
    assert backend.agent_calls == []


@pytest.mark.parametrize(
    "reply",
    [
        {"product": "Sony XM5", "tasks": ["execute_shell"]},
        {"product": "Sony XM5", "tasks": ["reviews"], "url": "https://evil.example"},
        "invalid-json",
    ],
)
async def test_invalid_llm_prediction_falls_back(reply):
    transport = httpx.MockTransport(
        lambda request: httpx.Response(
            200,
            json={
                "choices": [
                    {"message": {"content": reply if isinstance(reply, str) else json.dumps(reply)}}
                ],
            },
        )
    )
    async with httpx.AsyncClient(transport=transport) as http:
        prediction, source = await predict(
            PageContext(**PAGE), config(asi_one_api_key="test"), http
        )
    assert source == "rules"
    assert prediction.product == "Sony XM5"
    assert len(prediction.tasks) == 3


async def test_llm_routing_deduplicates_and_preserves_explicit_product():
    transport = httpx.MockTransport(
        lambda request: httpx.Response(
            200,
            json={
                "choices": [
                    {
                        "message": {
                            "content": json.dumps(
                                {
                                    "product": "Different product",
                                    "tasks": ["reviews", "reviews"],
                                }
                            )
                        }
                    }
                ],
            },
        )
    )
    async with httpx.AsyncClient(transport=transport) as http:
        prediction, source = await predict(
            PageContext(**PAGE), config(asi_one_api_key="test"), http
        )
    assert source == "asi_one"
    assert prediction.product == "Sony XM5"
    assert prediction.tasks == ["reviews"]


async def test_secret_key_never_replaces_auth_user_token():
    requests = []

    def handle(request):
        requests.append(request)
        return httpx.Response(200, json=[])

    async with httpx.AsyncClient(transport=httpx.MockTransport(handle)) as http:
        await Store(config(), http).tasks(USER, 20, 0)
    assert requests[0].headers["apikey"] == "secret-test"
    assert "authorization" not in requests[0].headers
    assert requests[0].url.params["user_id"] == f"eq.{USER}"


def asi_tool_call(name, arguments):
    message = {
        "role": "assistant",
        "content": "",
        "tool_calls": [
            {"id": "t1", "type": "function", "function": {"name": name, "arguments": arguments}}
        ],
    }
    return httpx.Response(200, json={"choices": [{"message": message}]})


def asi_text(text):
    return httpx.Response(
        200, json={"choices": [{"message": {"role": "assistant", "content": text}}]}
    )


def chat_client(backend, llm=None, **overrides):
    def handle(request):
        if request.url.host == "api.asi1.ai":
            return llm(json.loads(request.content))
        return backend.handle(request)

    http = httpx.AsyncClient(transport=httpx.MockTransport(handle))
    return TestClient(create_app(config(**overrides), http))


def test_chat_requires_auth_unless_disabled():
    backend = Backend()
    body = {"messages": [{"role": "user", "content": "Sony XM5 reviews"}]}
    with chat_client(backend) as client:
        assert client.post("/v1/chat", json=body).status_code == 401
    with chat_client(backend, chat_auth_required=False) as client:
        assert client.post("/v1/chat", json=body).status_code == 200


def test_chat_llm_routes_tools_to_agents():
    backend = Backend()
    seen = []

    def llm(request):
        seen.append(request)
        if len(seen) == 1:
            return asi_tool_call("get_reviews", '{"product": "Sony WH-1000XM5", "url": "http://x"}')
        assert request["messages"][-1]["role"] == "tool"
        return asi_text("Rated 4.6.")

    with chat_client(backend, llm, asi_one_api_key="k") as client:
        response = client.post(
            "/v1/chat",
            json={"messages": [{"role": "user", "content": "are the xm5 any good?"}]},
            headers={"Authorization": "Bearer valid"},
        )
    data = response.json()
    assert data["source"] == "asi_one" and data["reply"] == "Rated 4.6."
    assert backend.agent_calls == [("/reviews", {"product": "Sony WH-1000XM5"})]
    assert data["calls"][0]["kind"] == "reviews"


def test_chat_ignores_unknown_tools_and_falls_back_without_llm():
    backend = Backend()
    calls = 0

    def llm(request):
        nonlocal calls
        calls += 1
        if calls == 1:
            return asi_tool_call("fetch_url", '{"product": "x"}')
        return asi_text("Sorry.")

    with chat_client(backend, llm, asi_one_api_key="k", chat_auth_required=False) as client:
        body = {"messages": [{"role": "user", "content": "hi"}]}
        assert client.post("/v1/chat", json=body).json()["reply"] == "Sorry."
    assert backend.agent_calls == []

    with chat_client(backend, chat_auth_required=False) as client:
        body = {"messages": [{"role": "user", "content": "Sony XM5 price"}]}
        data = client.post("/v1/chat", json=body).json()
    assert data["source"] == "rules"
    # A shopping question always brings reviews along with the matched agent.
    assert [path for path, _ in backend.agent_calls] == ["/price", "/reviews"]


def test_chat_drops_prices_and_stores_the_user_never_said():
    backend = Backend()
    seen = []

    def llm(request):
        seen.append(request)
        if len(seen) == 1:
            args = '{"product": "AirPods Pro 2", "current_store": "Best Buy", "current_price": 249}'
            return asi_tool_call("compare_prices", args)
        return asi_text("Cheaper at Amazon.")

    with chat_client(backend, llm, asi_one_api_key="k", chat_auth_required=False) as client:
        text = "I'm on Best Buy looking at AirPods Pro 2. Cheaper anywhere?"
        client.post("/v1/chat", json={"messages": [{"role": "user", "content": text}]})
    assert backend.agent_calls == [
        ("/price", {"product": "AirPods Pro 2", "current_store": "Best Buy", "current_price": None})
    ]


def test_consider_routes_to_fetch_coordinator_and_exposes_partial_failures():
    seen = []

    def handle(request):
        assert request.url.path == "/consider"
        seen.append(json.loads(request.content))
        return httpx.Response(
            200,
            json={
                "product": "Sony WH-1000XM5",
                "max_price": 300,
                "summary": "Review sources found; price unavailable.",
                "price": None,
                "reviews": {"summary": "Review sources found", "sources": []},
                "alternatives": {"summary": "No verified offer", "alternatives": []},
                "errors": {"price": "Live price search unavailable"},
                "timings_ms": {"reviews": 123},
            },
        )

    http = httpx.AsyncClient(transport=httpx.MockTransport(handle))
    with TestClient(create_app(config(chat_auth_required=False), http)) as client:
        response = client.post(
            "/v1/chat",
            json={
                "messages": [
                    {
                        "role": "user",
                        "content": "Consider Sony XM5 and alternatives under $300",
                    }
                ]
            },
        )
    assert response.status_code == 200
    data = response.json()
    assert data["source"] == "coordinator" and len(data["calls"]) == 3
    assert data["calls"][0]["error"] == "Live price search unavailable"
    assert data["calls"][1]["ms"] == 123
    assert data["calls"][2]["payload"]["max_price"] == 300
    assert len(seen) == 1


async def test_agent_calls_retry_up_to_three_times(monkeypatch):
    from laai_orchestrator import chat
    from laai_orchestrator.models import TaskKind, ToolArgs

    monkeypatch.setattr(chat, "RETRY_DELAY", 0)
    attempts = []

    def handle(request):
        attempts.append(request.url.path)
        if len(attempts) == 1:
            return httpx.Response(503)
        if len(attempts) == 2:
            return httpx.Response(200, json={"summary": "s", "error": "search down"})
        return httpx.Response(200, json={"summary": "ok"})

    async with httpx.AsyncClient(transport=httpx.MockTransport(handle)) as http:
        call = await chat.call_agent(TaskKind.PRICE, ToolArgs(product="x"), config(), http)
    assert len(attempts) == 3 and call.result == {"summary": "ok"} and call.error is None

    attempts.clear()
    async with httpx.AsyncClient(
        transport=httpx.MockTransport(lambda r: attempts.append(1) or httpx.Response(503))
    ) as http:
        call = await chat.call_agent(TaskKind.PRICE, ToolArgs(product="x"), config(), http)
    assert len(attempts) == 3 and call.result is None and call.error


async def test_rules_add_reviews_to_shopping_questions():
    from laai_orchestrator import chat
    from laai_orchestrator.models import ChatMessage

    routes = []

    def handle(request):
        routes.append(request.url.path.strip("/"))
        return httpx.Response(200, json={"summary": request.url.path})

    async with httpx.AsyncClient(transport=httpx.MockTransport(handle)) as http:
        reply = await chat.chat_with_rules(
            [ChatMessage(role="user", content="I'm on Best Buy looking at AirPods Pro 2. Cheaper anywhere?")],
            config(),
            http,
        )
    assert sorted(routes) == ["alternatives", "price", "reviews"]
    assert [c.kind.value for c in reply.calls] == ["price", "alternatives", "reviews"]


@pytest.mark.parametrize(
    "text, coordinated",
    [
        ("Earbuds like the Galaxy Buds3 Pro but under $100", True),
        ("Something similar to the Sony XM5", True),
        ("Consider AirPods Pro", True),
        ("I'm on Best Buy looking at AirPods Pro 2. Cheaper anywhere?", True),
        ("Sony XM5 price", False),
        ("I'd like a review of the Sony XM5", False),
    ],
)
def test_budget_and_similar_product_questions_use_the_coordinator(text, coordinated):
    import re

    from laai_orchestrator.chat import COORDINATE, COORDINATE_WITHOUT_LLM

    # Without an LLM key (the local default), both patterns apply.
    found = re.search(COORDINATE, text, re.IGNORECASE) or re.search(
        COORDINATE_WITHOUT_LLM, text, re.IGNORECASE
    )
    assert bool(found) is coordinated


def _buyer_backend(seen):
    def handle(request):
        body = json.loads(request.content)
        seen.append((request.url.path, body))
        status = "cart_added" if request.url.path == "/buy/confirm" else "choose_listing"
        return httpx.Response(
            200,
            json={
                "product": "Sony WH-1000XM5" if "XM5" in body.get("product", "") else body.get("product", ""),
                "purchase_enabled": False,
                "status": status,
                "summary": "Select an Amazon listing.",
            },
        )

    return handle


async def test_add_it_to_cart_uses_the_cart_agent_with_the_discussed_product():
    from laai_orchestrator import chat
    from laai_orchestrator.models import ChatMessage

    seen = []
    history = [
        ChatMessage(role="user", content="Consider Sony XM5 and recommend an alternative under $300."),
        ChatMessage(role="assistant", content="Candidate under $300: Anker Soundcore Space Q45."),
        ChatMessage(role="user", content="add it to kart"),
    ]
    async with httpx.AsyncClient(transport=httpx.MockTransport(_buyer_backend(seen))) as http:
        reply = await chat.chat(history, config(), http)
    assert reply.source == "buyer"
    assert [path for path, _ in seen] == ["/buy/prepare"]
    assert "Sony XM5" in seen[0][1]["product"]
    assert reply.reply.startswith("Cart request for Sony WH-1000XM5.")


async def test_cart_request_naming_a_product_uses_that_product():
    from laai_orchestrator import chat
    from laai_orchestrator.models import ChatMessage

    seen = []
    history = [
        ChatMessage(role="user", content="Consider Sony XM5"),
        ChatMessage(role="user", content="Please add the Nothing Ear (a) to my cart"),
    ]
    async with httpx.AsyncClient(transport=httpx.MockTransport(_buyer_backend(seen))) as http:
        await chat.chat(history, config(), http)
    assert seen[0][0] == "/buy/prepare" and "Nothing Ear (a)" in seen[0][1]["product"]


async def test_add_it_to_cart_confirms_a_waiting_cart_approval():
    from laai_orchestrator import chat
    from laai_orchestrator.models import ChatMessage

    seen = []
    async with httpx.AsyncClient(transport=httpx.MockTransport(_buyer_backend(seen))) as http:
        reply = await chat.chat(
            [ChatMessage(role="user", content="Yes, add it to my cart")],
            config(),
            http,
            purchase_confirmation="a" * 32,
        )
    assert [path for path, _ in seen] == ["/buy/confirm"]
    assert reply.calls[0].result["status"] == "cart_added"


@pytest.mark.parametrize(
    "text",
    ["Add more detail to the review", "What does add to my budget mean?", "Sony XM5 price"],
)
def test_other_add_sentences_are_not_cart_requests(text):
    import re

    from laai_orchestrator.chat import CART_REQUEST

    assert not re.search(CART_REQUEST, text, re.IGNORECASE)


async def test_show_me_reviews_runs_only_the_reviews_agent():
    from laai_orchestrator import chat
    from laai_orchestrator.models import ChatMessage

    seen = []

    def handle(request):
        seen.append((request.url.path, json.loads(request.content)))
        return httpx.Response(200, json={"summary": "Experts rate it 4.6/5."})

    async with httpx.AsyncClient(transport=httpx.MockTransport(handle)) as http:
        reply = await chat.chat(
            [ChatMessage(role="user", content="Show me the reviews for the Sony XM5?")], config(), http
        )
    assert seen == [("/reviews", {"product": "Sony XM5"})]
    assert reply.reply == "Experts rate it 4.6/5."


async def test_coordinator_picks_reach_the_chat_reply():
    from laai_orchestrator import chat
    from laai_orchestrator.models import ChatMessage

    pick = {"label": "Best price", "product": "Sony CH-720N", "price": 99.99, "store": "bestbuy.com",
            "url": "https://www.bestbuy.com/site/1.p", "cart_url": "https://api.bestbuy.com/click/-/1/cart",
            "rating": 4.1, "reason": "Lowest trusted price"}

    def handle(request):
        return httpx.Response(200, json={"product": "Sony headphones", "summary": "s", "picks": [pick]})

    async with httpx.AsyncClient(transport=httpx.MockTransport(handle)) as http:
        reply = await chat.chat(
            [ChatMessage(role="user", content="Are there Sony headphones under $300?")], config(), http
        )
    assert reply.source == "coordinator" and reply.picks == [{**pick, "image": None}]


async def test_only_store_photos_reach_the_dashboard():
    from laai_orchestrator import chat
    from laai_orchestrator.models import ChatMessage

    amazon = "https://m.media-amazon.com/images/I/61abc.jpg"
    picks = [
        {"label": "Best price", "product": "A", "url": "https://www.amazon.com/dp/B0DFZPR9Z4",
         "reason": "r", "image": amazon},
        {"label": "Alternative", "product": "B", "url": "https://example.com/b",
         "reason": "r", "image": "https://tracker.example.com/pixel.gif"},
    ]

    def handle(request):
        return httpx.Response(200, json={"product": "Sony WH-1000XM5", "summary": "s",
                                         "image": "http://m.media-amazon.com/x.jpg", "picks": picks})

    async with httpx.AsyncClient(transport=httpx.MockTransport(handle)) as http:
        reply = await chat.chat(
            [ChatMessage(role="user", content="Consider Sony XM5 under $300")], config(), http
        )
    assert [p["image"] for p in reply.picks] == [amazon, None]
    assert reply.product == "Sony WH-1000XM5" and reply.image is None  # http is refused


async def test_reply_to_what_to_say_becomes_the_draft():
    from laai_orchestrator import chat
    from laai_orchestrator.models import ChatMessage

    seen = []

    def handle(request):
        seen.append(json.loads(request.content)["request"])
        return httpx.Response(200, json={"status": "ready", "summary": "Draft ready."})

    history = [
        ChatMessage(role="user", content="Hey send a message to jonathan"),
        ChatMessage(role="assistant", content="What would you like to say to Jonathan?"),
        ChatMessage(role="user", content="running 10 minutes late"),
    ]
    async with httpx.AsyncClient(transport=httpx.MockTransport(handle)) as http:
        reply = await chat.chat(history, config(), http)
    assert seen == ["text Jonathan running 10 minutes late"]
    assert reply.calls[0].kind == "message"


@pytest.mark.parametrize(
    "text, is_message",
    [
        ("Hey send a message to jonathan", True),
        ("send a text to Jonathan saying hi", True),
        ("Text Jonathan I'm outside", True),
        ("tell me about the Sony XM5", False),
        ("text scale is too small", False),
    ],
)
def test_message_requests_are_recognized(text, is_message):
    from laai_orchestrator.chat import _is_message_request

    assert _is_message_request(text) is is_message


def test_message_sent_accepts_only_sms_links():
    from pydantic import ValidationError

    from laai_orchestrator.models import MessageSent

    ok = MessageSent(status="open_link", summary="s", open_url="sms:+12137930070?&body=Hi.")
    assert ok.open_url.startswith("sms:+1213")
    with pytest.raises(ValidationError):
        MessageSent(status="open_link", summary="s", open_url="javascript:alert(1)")


def test_budget_routing_matches_the_coordinators_phrases():
    import re

    from laai_orchestrator.chat import COORDINATE

    for text in ("headphones less than $300", "earbuds max $80", "a tv maximum 500 dollars"):
        assert re.search(COORDINATE, text, re.IGNORECASE), text
    assert not re.search(COORDINATE, "iPhone 16 Pro Max 256GB price", re.IGNORECASE)


async def test_buyer_non_object_reply_is_an_agent_error():
    from types import SimpleNamespace

    from laai_orchestrator.chat import buyer_call

    class Http:
        async def post(self, url, **_):
            return httpx.Response(200, json=["not", "an", "object"], request=httpx.Request("POST", url))

    config = SimpleNamespace(agents_url="http://agents.test")
    call = await buyer_call({"product": "Sony XM5"}, config, Http())
    assert call.result is None and call.error == "Amazon cart agent unavailable"


@pytest.mark.parametrize(
    "text",
    ["Brief me on the news today.", "summarize to me everything that happened in United States yesterday",
     "give me a rundown of current events"],
)
def test_general_news_requests_route_to_news(text):
    import re

    from laai_orchestrator.chat import NEWS_WORDS

    assert re.search(NEWS_WORDS, text, re.IGNORECASE)


def test_brief_stories_are_sanitized():
    from laai_orchestrator.chat import safe_news

    result = safe_news({
        "summary": "News brief.", "brief": True, "headline": "News brief: Sunday",
        "stories": [
            {"section": "U.S.", "headline": "Real story", "summary": "Lead.", "url": "https://www.nbcnews.com/a",
             "publisher": "NBC News", "image_url": "javascript:alert(1)", "coverage": 5},
            {"section": "U.S.", "headline": "Bad link", "summary": "x", "url": "http://127.0.0.1/a"},
        ],
    })
    assert result["brief"] is True and [s["headline"] for s in result["stories"]] == ["Real story"]
    assert result["stories"][0]["image_url"] is None and result["stories"][0]["coverage"] == 5


def test_brief_story_paragraphs_pass_through_capped():
    from laai_orchestrator.chat import NEWS_BODY_MAX, safe_news

    story = {"section": "U.S.", "headline": "Story", "summary": "Lead.", "url": "https://www.nbcnews.com/a",
             "publisher": "NBC News", "body": "x" * 5000, "read_from": ["NBC News", "AP", "KSN", "Extra"]}
    result = safe_news({"summary": "s", "brief": True, "stories": [story]})
    assert len(result["stories"][0]["body"]) <= NEWS_BODY_MAX
    assert result["stories"][0]["read_from"] == ["NBC News", "AP", "KSN"]


# Audit 4: after "What would you like to say to X?", other requests and "never mind" are
# not turned into the message.
@pytest.mark.parametrize(
    "reply, expected",
    [
        ("running 10 minutes late", ["/imessage/prepare"]),
        ("Big news, I got the job!", ["/imessage/prepare"]),
        ("find the song Toxic by BoywithUke", ["/youtube"]),
        ("Brief me on the news today.", ["/news"]),
        ("never mind", []),
    ],
)
async def test_message_follow_up_only_takes_plain_replies(reply, expected):
    from laai_orchestrator import chat
    from laai_orchestrator.models import ChatMessage

    seen = []

    def handle(request):
        seen.append(request.url.path)
        return httpx.Response(200, json={"summary": "ok", "status": "ready", "sources": []})

    history = [
        ChatMessage(role="user", content="send a message to Jonathan"),
        ChatMessage(role="assistant", content="What would you like to say to Jonathan?"),
        ChatMessage(role="user", content=reply),
    ]
    async with httpx.AsyncClient(transport=httpx.MockTransport(handle)) as http:
        result = await chat.chat(history, config(), http)
    assert seen == expected
    if reply == "never mind":
        assert result.reply == "OK, nothing will be sent."


# Audit 5: a news result without a summary must not crash the chat.
def test_news_without_summary_does_not_crash():
    from laai_orchestrator.chat import safe_news

    assert safe_news({"headline": "x", "summary": None})["summary"] == ""
    assert safe_news({"headline": "x"})["summary"] == ""


@pytest.mark.parametrize(
    "text, coordinated",
    [
        ("Is there a better alteranate for the garmin 5 forerunning watch", True),
        ("any alternate to the Kindle", True),
        ("is there a better option than the Kindle", True),
        ("replacement for my AirPods", True),
        ("alter the font size", False),
    ],
)
def test_alternative_questions_with_typos_use_the_coordinator(text, coordinated):
    import re

    from laai_orchestrator.chat import COORDINATE, COORDINATE_WITHOUT_LLM

    found = re.search(COORDINATE, text, re.IGNORECASE) or re.search(COORDINATE_WITHOUT_LLM, text, re.IGNORECASE)
    assert bool(found) is coordinated


# The coordinator follows the ask: songs and videos go to YouTube whatever the title
# says, and a store constraint ("in amazon") goes to the coordinator.
@pytest.mark.parametrize(
    "text, expected",
    [
        ("Play Bad Guy by Billie Eilish", ["/youtube"]),
        ("Find a song like the Blinding Lights", ["/youtube"]),
        ("find the music video for Shape of You", ["/youtube"]),
        ("Are there any smart watches under 50 dollars in amazon", ["/consider"]),
        ("AirPods Pro 2 on amazon", ["/consider"]),
        ("video doorbells under 100 dollars", ["/consider"]),
    ],
)
async def test_routing_follows_what_was_asked(text, expected):
    from laai_orchestrator import chat
    from laai_orchestrator.models import ChatMessage

    seen = []

    def handle(request):
        seen.append(request.url.path)
        if request.url.path == "/consider":
            return httpx.Response(200, json={"product": "x", "summary": "ok", "mode": "browse",
                                             "price": {"summary": "ok", "offers": []}})
        return httpx.Response(200, json={"summary": "ok", "url": "https://www.youtube.com/watch?v=abcdefghijk"})

    async with httpx.AsyncClient(transport=httpx.MockTransport(handle)) as http:
        await chat.chat([ChatMessage(role="user", content=text)], config(), http)
    assert seen == expected


async def test_a_browse_answer_shows_only_the_listings_card():
    from laai_orchestrator import chat
    from laai_orchestrator.models import ChatMessage

    def handle(request):
        return httpx.Response(200, json={
            "product": "smart watches", "max_price": 50, "store": "amazon.com", "mode": "browse",
            "summary": "Yes. Amazon has smart watches under $50.",
            "price": {"product": "smart watches", "summary": "Yes.", "offers": []},
            "picks": [{"label": "Most popular", "product": "Redmi Watch 5 Active", "price": 43.49,
                       "store": "amazon.com", "url": "https://www.amazon.com/dp/B0DFZPR9Z4",
                       "reason": "4.5★"}],
        })

    text = "Are there any smart watches under 50 dollars in amazon"
    async with httpx.AsyncClient(transport=httpx.MockTransport(handle)) as http:
        reply = await chat.chat([ChatMessage(role="user", content=text)], config(), http)
    assert reply.source == "coordinator" and [c.kind for c in reply.calls] == ["price"]
    assert reply.picks[0]["product"] == "Redmi Watch 5 Active"


# The orchestrator decides: a message ask reaches the messenger agent however it's
# phrased, and before any shopping rule.
@pytest.mark.parametrize(
    "text",
    [
        'Send jonathan a message "lol"',
        "shoot alex a text saying im late",
        'text jonathan "lol"',
        "Send Jonathan a message about the cheaper AirPods",
    ],
)
async def test_message_asks_go_to_the_messenger(text):
    from laai_orchestrator import chat
    from laai_orchestrator.models import ChatMessage

    seen = []

    def handle(request):
        seen.append(request.url.path)
        return httpx.Response(200, json={"status": "ready", "summary": "Draft ready."})

    async with httpx.AsyncClient(transport=httpx.MockTransport(handle)) as http:
        await chat.chat([ChatMessage(role="user", content=text)], config(), http)
    assert seen == ["/imessage/prepare"]


@pytest.mark.parametrize("text", ["tell me about the Sony XM5", "Send me the best AirPods deal"])
async def test_shopping_asks_are_not_messages(text):
    from laai_orchestrator.chat import _is_message_request

    assert not _is_message_request(text)


def test_slow_chat_answers_504_before_the_gateway_gives_up(monkeypatch):
    import asyncio

    from laai_orchestrator import main

    # The gateway waits 90 s and the lens 95 s; the orchestrator must answer first.
    assert main.CHAT_DEADLINE < 90
    monkeypatch.setattr(main, "CHAT_DEADLINE", 0.05)

    async def slow(request):
        await asyncio.sleep(2)
        return httpx.Response(200, json={"summary": "late"})

    http = httpx.AsyncClient(transport=httpx.MockTransport(slow))
    with TestClient(create_app(config(chat_auth_required=False), http)) as client:
        response = client.post("/v1/chat", json={"messages": [{"role": "user", "content": "AirPods Pro price"}]})
    assert response.status_code == 504
    assert "Check the laptop" in response.json()["detail"]
