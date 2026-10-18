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
        if path == "/rest/v1/laai_actions":
            return httpx.Response(200, json=[])
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
